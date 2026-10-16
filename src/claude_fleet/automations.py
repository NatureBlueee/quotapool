"""Persistent, serial dashboard automations; no arbitrary shell execution."""
from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta

from . import lark_integration


def next_run(job, now):
    if job['schedule'] == 'interval':
        return now + job['minutes'] * 60
    hour, minute = map(int, job['at'].split(':'))
    target = datetime.fromtimestamp(now).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target.timestamp() <= now:
        target += timedelta(days=1)
    return target.timestamp()


class Automations:
    def __init__(self, runtime, sample):
        self.runtime, self.sample = runtime, sample
        self.path = lark_integration.settings_path(runtime).with_name('automations.sqlite3')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stop = threading.Event()
        self.started = False
        self.execution_lock = threading.Lock()
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, body TEXT NOT NULL, due REAL NOT NULL, state TEXT, running INTEGER DEFAULT 0)')
            db.execute('CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, job_id TEXT, name TEXT, at REAL, status TEXT, detail TEXT)')
            db.execute("UPDATE runs SET status='interrupted', detail='服务中断，发送结果未知；请核对飞书，系统不会自动重发' WHERE status='running'")
            db.execute('UPDATE jobs SET running=0')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def list(self):
        with self.connect() as db:
            jobs = [{**json.loads(r['body']), 'nextRun': r['due'], 'running': bool(r['running'])} for r in db.execute('SELECT * FROM jobs ORDER BY due')]
            runs = [dict(r) for r in db.execute('SELECT * FROM runs ORDER BY at DESC LIMIT 100')]
        return {'jobs': jobs, 'runs': runs, 'timezone': 'UTC' + datetime.now().astimezone().strftime('%z'), 'engine': 'running' if self.started and not self.stop.is_set() else 'stopped'}

    def save(self, value):
        job = {k: value.get(k) for k in ('name', 'kind', 'schedule', 'minutes', 'at', 'content', 'notify', 'recipientKeys', 'enabled', 'threshold')}
        job['name'] = str(job['name'] or '').strip()
        job['content'] = str(job['content'] or '').strip()
        if not 1 <= len(job['name']) <= 80 or len(job['content']) > 4000:
            raise ValueError('名称需为 1–80 字，消息不超过 4000 字')
        if job['kind'] not in ('reminder', 'digest', 'services', 'disk') or job['schedule'] not in ('interval', 'daily'):
            raise ValueError('请选择支持的任务与时间规则')
        job['minutes'] = int(job['minutes'] or 10)
        job['threshold'] = float(job['threshold'] or 20)
        if not 1 <= job['minutes'] <= 10080 or not math.isfinite(job['threshold']) or not 1 <= job['threshold'] <= 10000:
            raise ValueError('间隔需为 1–10080 分钟，磁盘阈值需为 1–10000 GB')
        job['at'] = str(job['at'] or '09:00')
        try:
            datetime.strptime(job['at'], '%H:%M')
        except ValueError:
            raise ValueError('每日时间格式应为 HH:MM')
        job['enabled'], job['notify'] = bool(job['enabled']), bool(job['notify'])
        selected = job['recipientKeys']
        if not isinstance(selected, list) or any(not isinstance(x, str) for x in selected):
            raise ValueError('收件人必须是已保存的对象列表')
        valid = {r['key'] for r in lark_integration.load_settings(self.runtime)['recipients']}
        if job['notify'] and (not selected or not set(selected) <= valid):
            raise ValueError('请先在飞书设置保存收件人，再选择通知对象')
        if job['kind'] == 'reminder' and not job['content']:
            raise ValueError('请输入提醒内容')
        job['id'] = str(value.get('id') or uuid.uuid4().hex)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT running FROM jobs WHERE id=?', (job['id'],)).fetchone()
            if old and old['running']:
                raise ValueError('任务正在执行，请结束后编辑')
            db.execute('INSERT OR REPLACE INTO jobs(id,body,due,state,running) VALUES(?,?,?,NULL,0)', (job['id'], json.dumps(job, ensure_ascii=False), next_run(job, time.time())))
        return job

    def delete(self, job_id):
        with self.connect() as db:
            if not db.execute('DELETE FROM jobs WHERE id=? AND running=0', (job_id,)).rowcount:
                raise ValueError('任务不存在或正在执行')

    def run(self, job_id, preview=False):
        if not self.execution_lock.acquire(blocking=False):
            raise ValueError('已有任务执行中，请稍后再试')
        run_id = uuid.uuid4().hex
        try:
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
                if not row:
                    raise ValueError('任务不存在')
                job = json.loads(row['body'])
                if not preview:
                    db.execute('UPDATE jobs SET running=1,due=? WHERE id=?', (next_run(job, time.time()), job_id))
                db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?)', (run_id, job_id, job['name'], time.time(), 'running', '正在检查'))
            state = None
            try:
                if job['kind'] == 'reminder':
                    message, state = job['content'], 'reminder'
                else:
                    sample = self.sample()
                    if job['kind'] == 'disk':
                        disks = sample.get('disks', [])
                        if not disks:
                            raise ValueError('没有可用的磁盘采样，本次状态未知')
                        low = [d['drive'] for d in disks if d['freeBytes'] / 1024**3 < job['threshold']]
                        state = ','.join(sorted(low)) or 'healthy'
                        message = ('空间不足：' + state if low else '磁盘空间已恢复正常') + '\n' + '\n'.join(f"{d['drive']} 剩余 {d['freeBytes']/1024**3:.1f} GB" for d in disks)
                    elif job['kind'] == 'services':
                        services = sample['services']
                        state = ','.join(k for k, v in services.items() if not v) or 'healthy'
                        message = '本地服务正常' if state == 'healthy' else '服务异常：' + state
                    else:
                        state = 'digest'
                        message = 'QuotaPool 运行摘要\n' + '\n'.join(f"{k}：{'在线' if v else '离线'}" for k, v in sample['services'].items())
                        message += '\n' + '\n'.join(f"{d['drive']} 剩余 {d['freeBytes']/1024**3:.1f} GB" for d in sample.get('disks', []))
                changed = job['kind'] in ('reminder', 'digest') or (state != row['state'] and (state != 'healthy' or row['state'] is not None))
                status = 'preview' if preview else 'success'
                if preview:
                    message = '试运行 · 未发送飞书\n' + message
                elif job['notify'] and changed:
                    valid = {r['key'] for r in lark_integration.load_settings(self.runtime)['recipients']}
                    if not set(job['recipientKeys']) <= valid:
                        raise ValueError('通知对象已被移除，请重新配置任务')
                    result = lark_integration.share(self.runtime, {'requestId': run_id, 'recipientKeys': job['recipientKeys'], 'title': job['name'], 'content': message, 'format': 'text'})
                    if not result.get('ok'):
                        # Preserve state after an uncertain/partial delivery; avoid repeating successful recipients.
                        status = 'failed'
                        message += '\n通知未全部送达：' + '；'.join(str(r.get('error', '')) for r in result['results'] if not r['ok'])
                    else:
                        message += f"\n已发送给 {result['sent']} 个对象"
                elif not changed:
                    status = 'unchanged'
                    message += '\n状态无变化，未发送通知'
            except Exception as exc:
                status, message = 'failed', str(exc)[:1000]
            with self.connect() as db:
                db.execute('UPDATE runs SET status=?,detail=? WHERE id=?', (status, message, run_id))
                if not preview:
                    db.execute('UPDATE jobs SET running=0,state=COALESCE(?,state) WHERE id=?', (state, job_id))
                db.execute('DELETE FROM runs WHERE id NOT IN (SELECT id FROM runs ORDER BY at DESC LIMIT 500)')
            return {'status': status, 'detail': message}
        finally:
            self.execution_lock.release()

    def loop(self):
        self.started = True
        while not self.stop.wait(5):
            try:
                self.tick()
            except Exception:
                # Keep the worker alive; run failures have their own history.
                continue

    def tick(self):
        for job in self.list()['jobs']:
            if job['enabled'] and job['nextRun'] <= time.time():
                self.run(job['id'])
