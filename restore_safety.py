"""Block index rollback that would discard unresolved physical-file history."""


def require_reconciled_files(connection,permit=None,role='current'):
    from restore_reconciliation import metadata_only,RestorePermit,journal
    if permit is not None:
        if not isinstance(permit,RestorePermit):raise ValueError('恢复需要经过验证的文件对账许可')
        permit.validate(connection,role);return
    if metadata_only(connection):return
    try:journal(connection)
    except (ValueError,KeyError,TypeError) as error:raise ValueError('整理日志不完整，不能恢复数据库') from error
    tables={row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'organization_jobs' in tables:
        if connection.execute("SELECT 1 FROM organization_jobs WHERE state IN ('queued','running','waiting','partial','interrupted') LIMIT 1").fetchone():
            raise ValueError('请先处理或取消整理任务，再恢复备份')
    if 'organization_steps' in tables:
        if connection.execute("SELECT 1 FROM organization_steps WHERE state!='pending' LIMIT 1").fetchone():
            raise ValueError('存在文件整理记录，请先完成文件日志对账；数据库恢复不会撤销文件操作')
