"""
WSGI config for archery project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/1.8/howto/deployment/wsgi/
"""

import os
import threading

from django.core.wsgi import get_wsgi_application

from common.utils.ding_api import dingtalk_stream_client_start
from sql.utils.tasks import add_sync_ding_user_by_mobile_schedule

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "archery.settings")
audit = os.environ.get('CURRENT_AUDITOR')
if audit == 'sql.utils.workflow_audit:DingTalkAudit':
    thread = threading.Thread(target=dingtalk_stream_client_start)
    thread.start()
    # 默认添加基于手机号的定时任务同步
    add_sync_ding_user_by_mobile_schedule()
application = get_wsgi_application()
