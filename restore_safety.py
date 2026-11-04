"""Block index rollback that would discard unresolved physical-file history."""


def require_reconciled_files(connection):
    tables={row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'organization_jobs' in tables:
        if connection.execute("SELECT 1 FROM organization_jobs WHERE state IN ('queued','running','waiting','partial','interrupted') LIMIT 1").fetchone():
            raise ValueError('请先处理或取消整理任务，再恢复备份')
    if 'organization_steps' in tables:
        if connection.execute("SELECT 1 FROM organization_steps WHERE state!='pending' LIMIT 1").fetchone():
            raise ValueError('存在文件整理记录，请先完成文件日志对账；数据库恢复不会撤销文件操作')
