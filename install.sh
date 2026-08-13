#!/bin/sh
# Install Quotapool on this machine.
#
#   ./install.sh --dry-run     print every change, make none
#   ./install.sh               do it
#   ./install.sh --uninstall   put the machine back
#   ./install.sh --no-services skip launchd/systemd (does not honour HOME)
#
# ## Why this file did not exist until now
#
# Every step below was already implemented — `quotapool settings install`,
# `quotapool shell install`, `bootstrap_config.py`, `packaging/systemd/install-systemd.sh`
# — as separate verbs, each careful, none of them assembled. The README opened
# with "当前本机状态", which is an operator's notebook: it describes a machine
# that already works rather than how to get one. Anyone cloning the repository
# had no path to a running system, and `pip install` succeeding meant nothing
# because the six wrappers that actually run Fleet were not in the repository at
# all until they were imported into `bin/`.
#
# ## Three rules, each from something that went wrong here
#
# 1. **Preflight before touching anything.** A half-finished install of a thing
#    that intercepts `claude` is worse than no install: the shim can be in place
#    while the service that gives it meaning is not.
# 2. **Never replace a file this script did not write.** Borrowed from
#    `install-systemd.sh`, which already refuses to overwrite a unit it did not
#    generate. A name collision is an error, not a silent takeover.
# 3. **Verify by using it, then say what was not verified.** `quotapool doctor`
#    reads files; it cannot tell you a request will route. The canary sends a
#    real one, and it needs an account, which needs a browser — so the install
#    ends by naming that gap rather than implying it is closed.
set -eu

PREFIX=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
# New installs use the project's own name. An existing `claude-fleet` directory
# is honoured instead, because that is where a running fleet's credentials,
# database and service definitions already live — renaming those in place would
# break a working system to make a directory listing tidier.
_config_root="${XDG_CONFIG_HOME:-$HOME/.config}"
_state_root="${XDG_STATE_HOME:-$HOME/.local/state}"
if [ -d "$_config_root/claude-fleet" ]; then
  CONFIG_DIR="$_config_root/claude-fleet"
  STATE_DIR="$_state_root/claude-fleet"
else
  CONFIG_DIR="$_config_root/quotapool"
  STATE_DIR="$_state_root/quotapool"
fi
BIN_DIR="$HOME/.local/bin"

DRY=0
UNINSTALL=0
# Services are the one step that is NOT contained by overriding HOME:
# `install-timers.sh` calls `launchctl bootout/bootstrap gui/$(id -u)`, which
# acts on the real login session whatever HOME says. So a test install into a
# scratch HOME would quietly replace the running jobs with ones pointing at the
# scratch directory. This flag is what makes such a test safe, and it is also
# what a machine whose services are managed elsewhere needs.
SKIP_SERVICES=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --no-services) SKIP_SERVICES=1 ;;
    -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) printf 'install.sh: unknown argument: %s\n' "$arg" >&2; exit 2 ;;
  esac
done

BOLD=$(printf '\033[1m'); DIM=$(printf '\033[2m')
RED=$(printf '\033[31m'); GREEN=$(printf '\033[32m'); YELLOW=$(printf '\033[33m')
RESET=$(printf '\033[0m')

say() { printf '%s\n' "$*"; }
step() { printf '\n%s%s%s\n' "$BOLD" "$*" "$RESET"; }
ok() { printf '  %s✓%s %s\n' "$GREEN" "$RESET" "$*"; }
warn() { printf '  %s!%s %s\n' "$YELLOW" "$RESET" "$*"; }
die() { printf '\n%s%s%s\n' "$RED" "$*" "$RESET" >&2; exit 1; }
would() { printf '  %s将执行:%s %s\n' "$DIM" "$RESET" "$*"; }

# A marker so uninstall only removes what this script created, and so a second
# install can tell its own symlink from something a person put there.
MARKER="claude-fleet-install"

# ---------------------------------------------------------------------------
# preflight — everything that must be true before one byte is written
# ---------------------------------------------------------------------------
preflight() {
  step "① 前置检查"
  missing=0

  if command -v python3 >/dev/null 2>&1; then
    pyver=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
    case "$pyver" in
      3.9|3.1[0-9]) ok "python3 $pyver" ;;
      *) warn "python3 $pyver —— 需要 3.9+"; missing=1 ;;
    esac
  else
    warn "找不到 python3"; missing=1
  fi

  NODE=$(command -v node 2>/dev/null || true)
  if [ -n "$NODE" ]; then
    nodemajor=$("$NODE" -p 'process.versions.node.split(".")[0]')
    if [ "$nodemajor" -ge 20 ]; then
      ok "node v$("$NODE" -p 'process.versions.node')"
    else
      warn "node v$("$NODE" -p 'process.versions.node') —— 数据面需要 20+"; missing=1
    fi
  else
    warn "找不到 node —— 数据面跑不起来"; missing=1
  fi

  # The Claude Code binary, not the Fleet shim. Storing the shim would make the
  # launcher call itself; bootstrap_config.py refuses that outright, and it is
  # cheaper to catch here with a sentence than there with a traceback.
  CLAUDE_BINARY=""
  versions="$HOME/.local/share/claude/versions"
  if [ -d "$versions" ]; then
    # Newest by version number, and only files with something in them: a failed
    # download leaves a zero-byte file that sorts highest.
    CLAUDE_BINARY=$(ls -1 "$versions" 2>/dev/null | sort -t. -k1,1n -k2,2n -k3,3n \
      | while read -r v; do [ -s "$versions/$v" ] && [ -x "$versions/$v" ] && printf '%s\n' "$versions/$v"; done \
      | tail -1)
  fi
  if [ -n "$CLAUDE_BINARY" ]; then
    ok "Claude Code $("$CLAUDE_BINARY" --version 2>/dev/null | head -1)"
  else
    # Braces are load-bearing here. `$versions（` makes some shells read the
    # full-width parenthesis as part of the variable name — the first byte of
    # its UTF-8 encoding is a valid name character to them — and under `set -u`
    # that aborts with "versions（: unbound variable". Only reachable on the
    # error path, so only a test that provokes the error could find it.
    warn "找不到 Claude Code 可执行文件（${versions}）"; missing=1
  fi

  if [ ! -d "$PREFIX/vendor/teamclaude/src" ]; then
    warn "数据面缺失: $PREFIX/vendor/teamclaude/src"; missing=1
  else
    ok "数据面源码在位"
  fi
  if [ ! -d "$PREFIX/bin" ]; then
    warn "包装脚本缺失: $PREFIX/bin"; missing=1
  else
    ok "$(ls -1 "$PREFIX/bin" | wc -l | tr -d ' ') 个包装脚本在位"
  fi

  case "$(uname -s)" in
    Darwin) ok "平台 macOS（launchd）" ;;
    Linux)  ok "平台 Linux（systemd）" ;;
    *) warn "不支持的平台: $(uname -s)"; missing=1 ;;
  esac

  [ "$missing" -eq 0 ] || die "前置检查未通过。上面每一条都要先解决 —— 装到一半的
Fleet 比没装更糟: 拦截 claude 的 shim 会在位, 而给它意义的服务不在。"
}

# ---------------------------------------------------------------------------
install_wrappers() {
  step "③ 安装包装脚本 → $BIN_DIR"
  mkdir -p "$BIN_DIR" 2>/dev/null || true
  for source in "$PREFIX"/bin/*; do
    name=$(basename "$source")
    target="$BIN_DIR/$name"
    if [ -e "$target" ] && [ ! -L "$target" ]; then
      # A real file here is either an older hand-made install of this same
      # thing, or somebody else's program with an unlucky name. Content decides,
      # not the name: identical bytes means replacing it with a symlink changes
      # no behaviour at all, and this machine is exactly that case — the
      # wrappers were copied by hand long before they were in the repository.
      # Anything that differs is a modification somebody made, and taking it
      # over would discard work without asking.
      if cmp -s "$source" "$target"; then
        if [ "$DRY" -eq 1 ]; then
          would "ln -sf $source $target   ${DIM}(内容一致的旧安装, 换成链接)${RESET}"
          continue
        fi
      else
        die "$target 已存在, 且内容与仓库里的不同 —— 拒绝覆盖。
     它可能是别人的程序, 也可能是有人改过的这一份。
     先 diff 再决定:
       diff $source $target"
      fi
    fi
    if [ "$DRY" -eq 1 ]; then would "ln -s $source $target"; continue; fi
    ln -sf "$source" "$target"
  done
  [ "$DRY" -eq 1 ] || ok "$(ls -1 "$PREFIX/bin" | wc -l | tr -d ' ') 个已链接"

  # `claude` itself. Deliberately last and deliberately loud: this is the line
  # that makes every future `claude` go through Fleet, and the one the escape
  # hatch exists for.
  if [ -e "$BIN_DIR/claude" ] && [ ! -L "$BIN_DIR/claude" ]; then
    warn "$BIN_DIR/claude 是真实文件, 不动它。Fleet 入口用 claude-pool。"
  elif [ "$DRY" -eq 1 ]; then
    would "ln -s claude-fleet-shell $BIN_DIR/claude   ${DIM}(此后 claude 走 Fleet)${RESET}"
  else
    ln -sf claude-fleet-shell "$BIN_DIR/claude"
    ok "claude → claude-fleet-shell  ${DIM}(坏了用 claude-direct)${RESET}"
  fi
}

bootstrap() {
  step "② 生成配置 → $CONFIG_DIR"
  if [ "$DRY" -eq 1 ]; then
    would "bootstrap_config.py --config-dir $CONFIG_DIR --state-dir $STATE_DIR"
    would "  （已存在的账号与协调器令牌会被保留, 不重新生成）"
    return
  fi
  mkdir -p "$CONFIG_DIR" "$STATE_DIR"
  token=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
  # `--token=$token`, not `--token $token`. `token_urlsafe` draws from the
  # URL-safe base64 alphabet, which includes `-`, so about one token in
  # sixty-four begins with one — and argparse then reads it as an option and
  # dies with `--token: expected one argument`. That is roughly one fresh
  # install in sixty-four failing at the config step with a usage message that
  # says nothing about what went wrong.
  #
  # It surfaced as a flaky test: twelve standalone runs clean, then two failures
  # in ten under pytest. A 1.5% failure on a first-run path is exactly the shape
  # that gets written off as flakiness and shipped.
  python3 "$PREFIX/scripts/bootstrap_config.py" \
    --config-dir "$CONFIG_DIR" --state-dir "$STATE_DIR" --prefix "$PREFIX" \
    --claude-binary "$CLAUDE_BINARY" --token="$token" --node-binary "$NODE" \
    ${OUTBOUND_PROXY:+--outbound-proxy="$OUTBOUND_PROXY"} >/dev/null
  ok "runtime.json 与 teamclaude.json  ${DIM}(0600)${RESET}"
}

install_services() {
  step "④ 安装服务"
  if [ "$SKIP_SERVICES" -eq 1 ]; then
    warn "按 --no-services 跳过 —— 服务没装, Fleet 不会自己起来"
    return
  fi
  case "$(uname -s)" in
    Darwin)
      if [ "$DRY" -eq 1 ]; then
        would "packaging/launchd/install-timers.sh"
        return
      fi
      # The two core services were installed by hand on the machine this grew
      # up on — `doctor` says so in as many words — and their plists carried an
      # absolute home directory, which is both unpublishable and wrong for
      # anybody else. They are templates now, and this is what fills them in.
      agents="$HOME/Library/LaunchAgents"
      mkdir -p "$agents"
      for template in "$PREFIX"/packaging/launchd/*.plist.in; do
        label=$(basename "$template" .plist.in)
        target="$agents/$label.plist"
        if [ -e "$target" ] && ! grep -q "$PREFIX\|$HOME" "$target" 2>/dev/null; then
          warn "$label.plist 已存在且不指向这个安装 —— 不动它"
          continue
        fi
        sed "s|@HOME@|$HOME|g" "$template" > "$target"
        plutil -lint "$target" >/dev/null 2>&1 \
          || die "生成的 $label.plist 不是合法 plist —— 已停下, 没有加载它"
      done
      ok "$(ls -1 "$PREFIX"/packaging/launchd/*.plist.in | wc -l | tr -d ' ') 个 plist 已生成并通过 plutil"
      say "  ${DIM}加载: launchctl bootstrap gui/\$(id -u) $agents/com.claude-fleet.<label>.plist${RESET}"
      say "  ${DIM}或用带影子验证与自动回滚的: fleet-reload-service <label> <plist>${RESET}"
      ;;
    Linux)
      say "  ${DIM}systemd 单元需要 root:${RESET}"
      say "    sudo sh $PREFIX/packaging/systemd/install-systemd.sh --user $(id -un)"
      say "  ${DIM}先看它会写什么: 同一条命令加 --dry-run${RESET}"
      ;;
  esac
}

install_hooks() {
  step "⑤ 接入 Claude Code"
  if [ "$DRY" -eq 1 ]; then
    would "quotapool settings install   ${DIM}(processWrapper + hooks)${RESET}"
    would "quotapool shell install      ${DIM}(PATH shim, 让嵌套 claude 也走 Fleet)${RESET}"
    return
  fi
  if "$BIN_DIR/quotapool" settings install \
       --wrapper "$BIN_DIR/claude-fleet-wrapper" \
       --fleet-binary "$BIN_DIR/quotapool" \
       --state-db "$STATE_DIR/fleet.sqlite3" >/dev/null 2>&1; then
    ok "Claude Code settings 覆盖已装  ${DIM}(processWrapper + hooks)${RESET}"
  else
    warn "settings 未装 —— 先看它想改什么:
     quotapool settings propose --wrapper $BIN_DIR/claude-fleet-wrapper \\
       --fleet-binary $BIN_DIR/quotapool --state-db $STATE_DIR/fleet.sqlite3"
  fi
  "$BIN_DIR/quotapool" shell install >/dev/null 2>&1 \
    && ok "shell shim 已装" || warn "shell shim 未装"
}

selfcheck() {
  step "⑥ 自检"
  if [ "$DRY" -eq 1 ]; then
    would "quotapool doctor"
    would "${DIM}（真实请求的验收需要先登录一个账号）${RESET}"
    return
  fi
  if "$BIN_DIR/quotapool" doctor >/dev/null 2>&1; then
    ok "doctor 通过"
  else
    warn "doctor 有失败项 —— 跑 quotapool doctor 看具体哪条"
  fi

  # Say the gap out loud. doctor reads files; it cannot tell you a request will
  # route, and the thing that could — the canary — needs an account, which needs
  # a browser. An install that implied otherwise would be lying by omission.
  printf '\n%s还没验证的:%s 真实请求能不能路由。doctor 只读文件。\n' "$YELLOW" "$RESET"
  say "  1) quotapool login              登录第一个账号（要浏览器）"
  say "  2) claude-fleet-canary A B  两个账号之间真跑一次切换"
  say "  3) claude                   正常使用"
  say ""
  say "  ${DIM}坏了怎么办: claude-direct 选账号直连 · fleet-rollback now 回退${RESET}"
}

uninstall() {
  step "卸载"
  for source in "$PREFIX"/bin/*; do
    name=$(basename "$source")
    target="$BIN_DIR/$name"
    # Only what points back at this checkout. A file, or a link somewhere else,
    # belongs to someone else.
    if [ -L "$target" ] && [ "$(readlink "$target")" = "$source" ]; then
      if [ "$DRY" -eq 1 ]; then would "rm $target"; else rm -f "$target"; fi
    fi
  done
  if [ -L "$BIN_DIR/claude" ] && [ "$(readlink "$BIN_DIR/claude")" = "claude-fleet-shell" ]; then
    if [ "$DRY" -eq 1 ]; then would "rm $BIN_DIR/claude   ${DIM}(原生 claude 恢复)${RESET}"
    else rm -f "$BIN_DIR/claude"; ok "claude 已恢复为原生"; fi
  fi
  if [ "$DRY" -eq 1 ]; then
    would "quotapool settings remove"
    would "quotapool shell remove"
  else
    "$BIN_DIR/quotapool" settings remove >/dev/null 2>&1 || true
    "$BIN_DIR/quotapool" shell remove >/dev/null 2>&1 || true
    ok "Claude Code settings 与 shim 已移除"
  fi
  case "$(uname -s)" in
    Darwin) say "  ${DIM}停服务: launchctl bootout gui/\$(id -u)/com.claude-fleet.{coordinator,dataplane}${RESET}" ;;
    Linux)  say "  ${DIM}停服务: sudo systemctl disable --now claude-fleet-coordinator claude-fleet-dataplane${RESET}" ;;
  esac
  printf '\n%s凭据与数据库保留在:%s\n' "$BOLD" "$RESET"
  say "  $CONFIG_DIR"
  say "  $STATE_DIR"
  say "  ${DIM}删掉别人的 OAuth 凭据是不可逆的, 所以留给你自己动手。${RESET}"
}

# ---------------------------------------------------------------------------
if [ "$UNINSTALL" -eq 1 ]; then
  uninstall
  exit 0
fi

[ "$DRY" -eq 1 ] && printf '%s干跑 —— 不会写任何东西%s\n' "$YELLOW" "$RESET"
preflight
bootstrap
install_wrappers
install_services
install_hooks
selfcheck
[ "$DRY" -eq 1 ] && printf '\n%s干跑结束, 什么都没改。%s\n' "$YELLOW" "$RESET"
exit 0
