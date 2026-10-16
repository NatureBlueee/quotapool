const VERSION = 2;
const MAX_DAYS = 400;
const MAX_WASTE_EVENTS = 200;

const QUOTA_BUCKETS = [
  ['fiveHour', '5H', 'unified5h', 'unified5hReset'],
  ['weekly', '7D', 'unified7d', 'unified7dReset'],
  ['fableWeekly', 'Fable 7D', 'unified7dFable', 'unified7dFableReset'],
];

function finiteNonNegative(value) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : 0;
}

function ratio(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return null;
  return Math.max(0, Math.min(1, number));
}

function timestamp(value) {
  if (value == null || value === '') return null;
  const number = Number(value);
  if (Number.isFinite(number)) return number;
  const parsed = new Date(value).getTime();
  return Number.isFinite(parsed) ? parsed : null;
}

function dayKey(value = Date.now()) {
  const date = new Date(value);
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

function emptyCounter() {
  return {
    requests: 0,
    inputTokens: 0,
    outputTokens: 0,
    successful: 0,
    interrupted: 0,
  };
}

function addCounter(target, source) {
  for (const key of Object.keys(emptyCounter())) {
    target[key] = finiteNonNegative(target[key]) + finiteNonNegative(source[key]);
  }
  return target;
}

function cleanName(value, fallback) {
  const text = String(value || '').trim();
  return (text || fallback).slice(0, 160);
}

function normalizedCounter(value = {}) {
  return addCounter(emptyCounter(), value);
}

function normalizedState(raw) {
  const source = raw && typeof raw === 'object' ? raw : {};
  const days = {};
  for (const [key, value] of Object.entries(source.days || {})) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(key) || !value || typeof value !== 'object') continue;
    days[key] = {
      ...normalizedCounter(value),
      accounts: Object.fromEntries(Object.entries(value.accounts || {}).map(
        ([name, counter]) => [cleanName(name, 'unknown'), normalizedCounter(counter)],
      )),
      models: Object.fromEntries(Object.entries(value.models || {}).map(
        ([name, counter]) => [cleanName(name, 'unknown'), normalizedCounter(counter)],
      )),
    };
  }
  return {
    version: VERSION,
    startedAt: source.startedAt || new Date().toISOString(),
    totals: normalizedCounter(source.totals),
    days,
    quotaWindows: source.quotaWindows && typeof source.quotaWindows === 'object'
      ? source.quotaWindows : {},
    quotaDaily: source.quotaDaily && typeof source.quotaDaily === 'object'
      ? source.quotaDaily : {},
    wasteEvents: Array.isArray(source.wasteEvents)
      ? source.wasteEvents.slice(-MAX_WASTE_EVENTS) : [],
  };
}

export class UsageAnalytics {
  constructor(state = null) {
    this.state = normalizedState(state);
    this.dirty = false;
  }

  recordRequest({
    at = Date.now(), account, model, inputTokens = 0, outputTokens = 0,
    status = 0, interrupted = false,
  } = {}) {
    const input = finiteNonNegative(inputTokens);
    const output = finiteNonNegative(outputTokens);
    const code = Number(status) || 0;
    const counter = {
      requests: 1,
      inputTokens: input,
      outputTokens: output,
      successful: !interrupted && code >= 200 && code < 400 ? 1 : 0,
      interrupted: interrupted ? 1 : 0,
    };
    const key = dayKey(at);
    const day = this.state.days[key] || {
      ...emptyCounter(), accounts: {}, models: {},
    };
    addCounter(day, counter);
    const accountName = cleanName(account, '未分配');
    const modelName = cleanName(model, '未知模型');
    day.accounts[accountName] = addCounter(
      normalizedCounter(day.accounts[accountName]), counter,
    );
    day.models[modelName] = addCounter(
      normalizedCounter(day.models[modelName]), counter,
    );
    this.state.days[key] = day;
    addCounter(this.state.totals, counter);
    this._pruneDays();
    this.dirty = true;
  }

  seedAccountTotals(accounts = [], { at = Date.now() } = {}) {
    if (this.state.totals.requests > 0 || Object.keys(this.state.days).length) {
      return false;
    }
    const key = dayKey(at);
    const day = { ...emptyCounter(), accounts: {}, models: {} };
    for (const account of accounts || []) {
      const usage = account?.usage || {};
      const requests = finiteNonNegative(usage.totalRequests);
      const inputTokens = finiteNonNegative(usage.totalInputTokens);
      const outputTokens = finiteNonNegative(usage.totalOutputTokens);
      if (!requests && !inputTokens && !outputTokens) continue;
      const counter = {
        requests,
        inputTokens,
        outputTokens,
        // Legacy counters did not distinguish result status. Keep the request
        // total accurate without inventing historical interruption data.
        successful: 0,
        interrupted: 0,
      };
      const accountName = cleanName(account?.name, '未分配');
      day.accounts[accountName] = addCounter(
        normalizedCounter(day.accounts[accountName]), counter,
      );
      addCounter(day, counter);
    }
    if (!day.requests && !day.inputTokens && !day.outputTokens) return false;
    day.models['启用前汇总（模型未知）'] = normalizedCounter(day);
    this.state.days[key] = day;
    addCounter(this.state.totals, day);
    this.dirty = true;
    return true;
  }

  observeQuotas(accounts = [], now = Date.now()) {
    let changed = false;
    for (const account of accounts || []) {
      const accountId = cleanName(account?.id || account?.accountUuid || account?.name, 'unknown');
      const accountName = cleanName(account?.name, accountId);
      const quota = account?.quota || {};
      for (const [bucket, label, usageKey, resetKey] of QUOTA_BUCKETS) {
        const key = `${accountId}:${bucket}`;
        const current = {
          accountId,
          account: accountName,
          bucket,
          label,
          utilization: ratio(quota[usageKey]),
          resetAt: timestamp(quota[resetKey]),
          observedAt: now,
        };
        const previous = this.state.quotaWindows[key];
        const advanced = previous?.resetAt && current.resetAt
          && current.resetAt > previous.resetAt + 1000;
        const expiredAndCleared = previous?.resetAt
          && previous.resetAt <= now && current.resetAt == null;
        let consumedRatio = 0;
        if ((advanced || expiredAndCleared) && previous.utilization != null) {
          this._recordWaste(previous, previous.resetAt);
          changed = true;
        }
        if (advanced && current.utilization != null) {
          consumedRatio = current.utilization;
        } else if (previous?.utilization != null && current.utilization != null
            && current.utilization > previous.utilization) {
          consumedRatio = current.utilization - previous.utilization;
        }
        if (consumedRatio > 0) {
          this._recordQuotaConsumption(current, consumedRatio, now);
          changed = true;
        }
        if (current.resetAt != null || current.utilization != null) {
          if (!previous
              || previous.resetAt !== current.resetAt
              || previous.utilization !== current.utilization
              || previous.account !== current.account) {
            this.state.quotaWindows[key] = current;
            changed = true;
          }
        } else if (previous && expiredAndCleared) {
          delete this.state.quotaWindows[key];
          changed = true;
        }
      }
    }
    if (changed) this.dirty = true;
    return changed;
  }

  _recordQuotaConsumption(window, consumedRatio, at) {
    const date = dayKey(at);
    const key = `${window.accountId}:${window.bucket}`;
    const daily = this.state.quotaDaily[date] || {};
    const previous = daily[key] || {};
    daily[key] = {
      accountId: window.accountId,
      account: window.account,
      bucket: window.bucket,
      label: window.label,
      consumedRatio: finiteNonNegative(previous.consumedRatio)
        + finiteNonNegative(consumedRatio),
      observations: finiteNonNegative(previous.observations) + 1,
      lastAt: new Date(at).toISOString(),
    };
    this.state.quotaDaily[date] = daily;
    this._pruneQuotaDaily();
  }

  _recordWaste(window, at) {
    const usedRatio = ratio(window.utilization);
    if (usedRatio == null) return;
    const remainingRatio = Math.max(0, 1 - usedRatio);
    this.state.wasteEvents.push({
      at: new Date(at || Date.now()).toISOString(),
      accountId: window.accountId,
      account: window.account,
      bucket: window.bucket,
      label: window.label,
      usedRatio,
      remainingRatio,
    });
    if (this.state.wasteEvents.length > MAX_WASTE_EVENTS) {
      this.state.wasteEvents.splice(
        0, this.state.wasteEvents.length - MAX_WASTE_EVENTS,
      );
    }
  }

  _pruneDays() {
    const keys = Object.keys(this.state.days).sort();
    for (const key of keys.slice(0, Math.max(0, keys.length - MAX_DAYS))) {
      delete this.state.days[key];
    }
  }

  _pruneQuotaDaily() {
    const keys = Object.keys(this.state.quotaDaily).sort();
    for (const key of keys.slice(0, Math.max(0, keys.length - MAX_DAYS))) {
      delete this.state.quotaDaily[key];
    }
  }

  _quotaEfficiency(now = Date.now()) {
    const yesterdayDate = new Date(now);
    yesterdayDate.setDate(yesterdayDate.getDate() - 1);
    const yesterday = this.state.quotaDaily[dayKey(yesterdayDate)] || {};
    const today = this.state.quotaDaily[dayKey(now)] || {};
    const build = (bucket, label) => {
      const accounts = [];
      let remainingRatio = 0;
      let requiredDailyRatio = 0;
      let yesterdayConsumedRatio = 0;
      let todayConsumedRatio = 0;
      for (const [key, window] of Object.entries(this.state.quotaWindows)) {
        if (window?.bucket !== bucket) continue;
        const utilization = ratio(window.utilization);
        const resetAt = timestamp(window.resetAt);
        if (utilization == null || resetAt == null || resetAt <= now) continue;
        const hoursLeft = (resetAt - now) / 3_600_000;
        const remaining = Math.max(0, 1 - utilization);
        const perDay = remaining / Math.max(hoursLeft / 24, 1 / 24);
        const yesterdayConsumed = finiteNonNegative(yesterday[key]?.consumedRatio);
        const todayConsumed = finiteNonNegative(today[key]?.consumedRatio);
        remainingRatio += remaining;
        requiredDailyRatio += perDay;
        yesterdayConsumedRatio += yesterdayConsumed;
        todayConsumedRatio += todayConsumed;
        accounts.push({
          account: window.account,
          remainingRatio: remaining,
          resetAt: new Date(resetAt).toISOString(),
          hoursLeft,
          requiredDailyRatio: perDay,
          yesterdayConsumedRatio: yesterdayConsumed,
          multiplier: yesterdayConsumed > 0 ? perDay / yesterdayConsumed : null,
        });
      }
      accounts.sort((a, b) => b.requiredDailyRatio - a.requiredDailyRatio);
      return {
        bucket,
        label,
        accounts: accounts.length,
        remainingRatio,
        requiredDailyRatio,
        yesterdayConsumedRatio,
        todayConsumedRatio,
        multiplier: yesterdayConsumedRatio > 0
          ? requiredDailyRatio / yesterdayConsumedRatio : null,
        earliestResetAt: accounts.length
          ? accounts.reduce((value, account) => (
            !value || account.resetAt < value ? account.resetAt : value
          ), null) : null,
        byAccount: accounts,
      };
    };
    return {
      available: true,
      generatedAt: new Date(now).toISOString(),
      yesterday: dayKey(yesterdayDate),
      weekly: build('weekly', '7D'),
      fiveHour: build('fiveHour', '5H'),
    };
  }

  exportState() {
    this.dirty = false;
    return JSON.parse(JSON.stringify(this.state));
  }

  snapshot({ days = 120, now = Date.now() } = {}) {
    const selected = Object.entries(this.state.days)
      .sort(([a], [b]) => a.localeCompare(b))
      .slice(-Math.max(1, Math.min(MAX_DAYS, Number(days) || 120)))
      .map(([date, value]) => ({ date, ...normalizedCounter(value) }));
    const today = normalizedCounter(this.state.days[dayKey(now)]);
    const dimensions = (field) => {
      const result = {};
      for (const day of Object.values(this.state.days)) {
        for (const [name, counter] of Object.entries(day[field] || {})) {
          result[name] = addCounter(normalizedCounter(result[name]), counter);
        }
      }
      return Object.entries(result)
        .map(([name, counter]) => ({ name, ...counter }))
        .sort((a, b) => b.requests - a.requests || b.outputTokens - a.outputTokens);
    };
    const byBucket = {};
    let totalRemainingRatio = 0;
    for (const event of this.state.wasteEvents) {
      const bucket = event.bucket || 'unknown';
      const item = byBucket[bucket] || {
        bucket, label: event.label || bucket, windows: 0, remainingRatio: 0,
      };
      item.windows += 1;
      item.remainingRatio += finiteNonNegative(event.remainingRatio);
      totalRemainingRatio += finiteNonNegative(event.remainingRatio);
      byBucket[bucket] = item;
    }
    const windows = this.state.wasteEvents.length;
    return {
      available: true,
      startedAt: this.state.startedAt,
      totals: normalizedCounter(this.state.totals),
      today,
      days: selected,
      accounts: dimensions('accounts'),
      models: dimensions('models'),
      waste: {
        windows,
        totalRemainingRatio,
        averageRemainingRatio: windows ? totalRemainingRatio / windows : 0,
        byBucket: Object.values(byBucket),
        recent: this.state.wasteEvents.slice(-20).reverse(),
        unit: 'quota-window-ratio',
      },
      quotaEfficiency: this._quotaEfficiency(now),
    };
  }
}

export { dayKey };
