#!/usr/bin/env python
# -*- coding: utf-8 -*-
import logging
import pickle
import threading
import time
from dataclasses import dataclass
from typing import Optional

import dingtalk_stream
import requests
from dataclasses_json import dataclass_json
from django.core.cache import cache
from django.http import JsonResponse

from archery import settings
from archery.settings import env
from common.config import SysConfig
from common.utils.permission import superuser_required
from sql.models import Users
from sql.utils.tasks import add_sync_ding_user_schedule

logger = logging.getLogger("default")
DEFAULT_TTL = 60 * 60 * 24


def get_access_token(app_key=None, app_secret=None):
    """获取access_token:https://ding-doc.dingtalk.com/doc#/serverapi2/eev437"""
    # 优先获取缓存
    try:
        access_token = cache.get("ding_access_token")
    except Exception as e:
        logger.error(f"获取钉钉access_token缓存出错:{e}")
        access_token = None
    if access_token:
        return access_token
    # 请求钉钉接口获取
    sys_config = SysConfig()
    app_key = app_key if app_key else sys_config.get("ding_app_key")
    app_secret = app_secret if app_secret else sys_config.get("ding_app_secret")
    url = f"https://oapi.dingtalk.com/gettoken?appkey={app_key}&appsecret={app_secret}"
    resp = requests.get(url, timeout=3).json()
    if resp.get("errcode") == 0:
        access_token = resp.get("access_token")
        expires_in = resp.get("expires_in")
        cache.set("ding_access_token", access_token, expires_in - 60)
        return access_token
    else:
        logger.error(f"获取钉钉access_token出错:{resp}")
        return None


def get_ding_user_id(username):
    """更新用户ding_user_id"""
    try:
        ding_user_id = cache.get(username.lower())
        if ding_user_id:
            user = Users.objects.get(username=username)
            if user.ding_user_id != str(ding_user_id, encoding="utf8"):
                user.ding_user_id = str(ding_user_id, encoding="utf8")
                user.save(update_fields=["ding_user_id"])
    except Exception as e:
        logger.error(f"更新用户ding_user_id失败:{e}")


def get_dept_list_id_fetch_child(token, parent_dept_id):
    """获取所有子部门列表"""
    ids = [int(parent_dept_id)]
    url = (
        "https://oapi.dingtalk.com/department/list_ids?id={0}&access_token={1}".format(
            parent_dept_id, token
        )
    )
    resp = requests.get(url, timeout=3).json()
    if resp.get("errcode") == 0:
        for dept_id in resp.get("sub_dept_id_list"):
            ids.extend(get_dept_list_id_fetch_child(token, dept_id))
    return list(set(ids))


def sync_ding_user_id():
    """
    使用工号（username）登陆archery，并且工号对应钉钉系统中字段 "jobnumber"。
    所以可根据钉钉中 jobnumber 查到该用户的 ding_user_id。
    """
    sys_config = SysConfig()
    ding_dept_ids = sys_config.get("ding_dept_ids", "")
    username2ding = sys_config.get("ding_archery_username")
    token = get_access_token()
    if not token:
        return False
    # 获取全部部门列表
    sub_dept_id_list = []
    for dept_id in list(set(ding_dept_ids.split(","))):
        sub_dept_id_list.extend(get_dept_list_id_fetch_child(token, dept_id))
    # 遍历部门下的用户
    user_ids = []
    for sdi in sub_dept_id_list:
        url = f"https://oapi.dingtalk.com/user/getDeptMember?access_token={token}&deptId={sdi}"
        try:
            resp = requests.get(url, timeout=3).json()
            if resp.get("errcode") == 0:
                user_ids.extend(resp.get("userIds"))
            else:
                raise Exception(f"获取部门用户出错:{resp}")
        except Exception as e:
            raise Exception(f"获取部门用户出错:{e}")
    # 获取所有用户信息并缓存
    for user_id in list(set(user_ids)):
        url = (
            f"https://oapi.dingtalk.com/user/get?access_token={token}&userid={user_id}"
        )
        try:
            resp = requests.get(url, timeout=3).json()
            if resp.get("errcode") == 0:
                if not resp.get(username2ding):
                    raise Exception(
                        f"钉钉用户信息不包含{username2ding}字段，无法获取id信息，请确认ding_archery_username配置{resp}"
                    )
                cache.set(resp.get(username2ding).lower(), resp.get("userId"), 86400)
            else:
                raise Exception(f"获取用户信息出错:{resp}")
        except Exception as e:
            raise Exception(f"获取用户信息出错:{e}")
    return True


def create_process(process_code, workflow) -> str:
    pass


def get_all_visiable_bpms_process(username):
    """
    根据当前用户获取其能查看的所有可管理的表单
    """

    user = Users.objects.get(username=username)
    if not user.ding_user_id:
        return []
    user_id = user.ding_user_id
    # 先从 redis 获取结果， redis 缓存时间为 60s
    try:
        user_bpms = cache.get(f"ding_bpms:{user_id}")
        if user_bpms:
            return pickle.loads(user_bpms)
    except Exception as e:
        raise Exception(f"获取 [钉钉审批表单] redis 缓存失败: {e}")

    AUTH_DINGDING_APP_KEY = env("AUTH_DINGDING_APP_KEY")
    AUTH_DINGDING_APP_SECRET = env("AUTH_DINGDING_APP_SECRET")
    token = get_access_token(app_key=AUTH_DINGDING_APP_KEY, app_secret=AUTH_DINGDING_APP_SECRET)
    offset = 0
    size = 100
    datas = []
    while True:
        url = f'https://oapi.dingtalk.com/topapi/process/listbyuserid?access_token={token}'
        body = {
            'userid': user_id,
            'offset': offset,
            'size': size,
        }
        try:
            resp = requests.post(url, data=body, timeout=3).json()
            if resp.get("errcode") == 0:
                _data = resp.get("result")
                datas.extend(_data.get("process_list", []))
                if _data.get("next_cursor", None):
                    offset = _data.get("next_cursor")
                else:
                    break
            else:
                break
        except Exception as e:
            logger.error(f"获取 [钉钉审批表单] 失败: {e}")
            cache.set(f"ding_bpms:{user_id}", [], timeout=DEFAULT_TTL)
            return []
    rd_data = pickle.dumps(datas)
    try:
        cache.set(f"ding_bpms:{user_id}", rd_data, timeout=DEFAULT_TTL)
        for item in datas:
            cache.set(f"ding_bpms:code:{item['process_code']}", item['name'], timeout=DEFAULT_TTL)
    except Exception as re:
        logger.error(f"设置 [钉钉审批表单] redis 缓存失败: {re}")
    return rd_data


def get_process_code_name(process_code, username) -> Optional[str]:
    name = cache.get(f'ding_bpms:code:{process_code}')
    if not name:
        items = get_all_visiable_bpms_process(username)
        name = cache.get(f'ding_bpms:code:{process_code}')
        if not name:
            for item in items:
                if item.get('process_code', '') == process_code:
                    cache.set(f"ding_bpms:code:{process_code}", item['name'], timeout=DEFAULT_TTL)
                    return item['name']
    return name


class ArcheryDingtalkEventHandler(dingtalk_stream.EventHandler):
    """
    钉钉事件处理器
        bpms_instance_change: 实例状态变更 记录审核状态用
        bpms_task_change: 实例下的 任务信息变更, 记录操作日志用
        [
  {
    "processInstanceId": "bhzP9bPWRdqZ34ek1uQ1YA06511723623364",
    "eventId": "d25eb728b406454f964ba911547456a7",
    "resource": "/v1.0/event/bpms_instance_change/processCode/PROC-509DCE81-B3E8-4AC3-AF86-06D24384DA92/type/start",
    "createTime": 1723623364000,
    "processCode": "PROC-509DCE81-B3E8-4AC3-AF86-06D24384DA92",
    "bizCategoryId": "",
    "businessId": "202408141616000043002",
    "title": "徐祥123提交的[测试]流程审批表单",
    "type": "start",
    "businessType": "",
    "url": "https://aflow.dingtalk.com/dingtalk/mobile/homepage.htm?corpid=dinga16416085ea853aef5bf40eda33b7ba0&dd_share=false&showmenu=false&dd_progress=false&back=native&procInstId=bhzP9bPWRdqZ34ek1uQ1YA06511723623364&taskId=&swfrom=isv&dinghash=approval&dtaction=os&dd_from=#approval",
    "staffId": "0334555956789461",
  },
  {
    "processInstanceId": "bhzP9bPWRdqZ34ek1uQ1YA06511723623364",
    "eventId": "8217f875ef364ee9a3e404d049275920",
    "resource": "/v1.0/event/bpms_task_change/processCode/PROC-509DCE81-B3E8-4AC3-AF86-06D24384DA92/type/start",
    "businessId": "202408141616000043002",
    "activityName": "审批人",
    "title": "徐祥123提交的[测试]流程审批表单",
    "type": "start",
    "activityId": "1918_5cd3",
    "createTime": 1723623364000,
    "processCode": "PROC-509DCE81-B3E8-4AC3-AF86-06D24384DA92",
    "bizCategoryId": "",
    "businessType": "",
    "staffId": "0334555956789461",
    "taskId": 88382752389,
  },
  {
    "processInstanceId": "bhzP9bPWRdqZ34ek1uQ1YA06511723623364",
    "eventId": "5b645d9f71e946c28c76cdc4b539f96a",
    "resource": "/v1.0/event/bpms_task_change/processCode/PROC-509DCE81-B3E8-4AC3-AF86-06D24384DA92/type/start",
    "businessId": "202408141616000043002",
    "activityName": "审批人",
    "title": "徐祥123提交的[测试]流程审批表单",
    "type": "start",
    "activityId": "f7f8_8547",
    "createTime": 1723624717000,
    "processCode": "PROC-509DCE81-B3E8-4AC3-AF86-06D24384DA92",
    "bizCategoryId": "",
    "businessType": "",
    "staffId": "0334555956789461",
    "taskId": 88383312935,
  },
  {
    "processInstanceId": "bhzP9bPWRdqZ34ek1uQ1YA06511723623364",
    "eventId": "414bc98c9d6944c1a00bac0bdbbec6dd",
    "finishTime": 1723624717000,
    "resource": "/v1.0/event/bpms_task_change/processCode/PROC-509DCE81-B3E8-4AC3-AF86-06D24384DA92/type/finish",
    "businessId": "202408141616000043002",
    "activityName": "审批人",
    "title": "徐祥123提交的[测试]流程审批表单",
    "type": "finish",
    "result": "agree",
    "activityId": "1918_5cd3",
    "createTime": 1723623364000,
    "processCode": "PROC-509DCE81-B3E8-4AC3-AF86-06D24384DA92",
    "bizCategoryId": "",
    "businessType": "",
    "staffId": "0334555956789461",
    "taskId": 88382752389,
  },
  {
    "processInstanceId": "bhzP9bPWRdqZ34ek1uQ1YA06511723623364",
    "eventId": "b417709536d748c4b485966e94e94ca3",
    "finishTime": 1723624798000,
    "resource": "/v1.0/event/bpms_instance_change/processCode/PROC-509DCE81-B3E8-4AC3-AF86-06D24384DA92/type/finish",
    "businessId": "202408141616000043002",
    "title": "徐祥123提交的[测试]流程审批表单",
    "type": "finish",
    "url": "https://aflow.dingtalk.com/dingtalk/mobile/homepage.htm?corpid=dinga16416085ea853aef5bf40eda33b7ba0&dd_share=false&showmenu=false&dd_progress=false&back=native&procInstId=bhzP9bPWRdqZ34ek1uQ1YA06511723623364&taskId=&swfrom=isv&dinghash=approval&dtaction=os&dd_from=#approval",
    "result": "refuse",
    "createTime": 1723623364000,
    "processCode": "PROC-509DCE81-B3E8-4AC3-AF86-06D24384DA92",
    "bizCategoryId": "",
    "businessType": "",
    "staffId": "0334555956789461",
  },
  {
    "processInstanceId": "bhzP9bPWRdqZ34ek1uQ1YA06511723623364",
    "eventId": "f61ac34d812e43edb34654f7a3579107",
    "finishTime": 1723624798000,
    "resource": "/v1.0/event/bpms_task_change/processCode/PROC-509DCE81-B3E8-4AC3-AF86-06D24384DA92/type/finish",
    "businessId": "202408141616000043002",
    "activityName": "审批人",
    "title": "徐祥123提交的[测试]流程审批表单",
    "type": "finish",
    "result": "refuse",
    "activityId": "f7f8_8547",
    "createTime": 1723624717000,
    "processCode": "PROC-509DCE81-B3E8-4AC3-AF86-06D24384DA92",
    "bizCategoryId": "",
    "businessType": "",
    "staffId": "0334555956789461",
    "taskId": 88383312935,
  },
]

    """

    async def process(self, event: dingtalk_stream.EventMessage):
        self.logger.info(
            'received event, delay=%sms, eventType=%s, eventId=%s, eventBornTime=%d, eventCorpId=%s, '
            'eventUnifiedAppId=%s, data=%s',
            int(time.time() * 1000) - event.headers.event_born_time,
            event.headers.event_type,
            event.headers.event_id,
            event.headers.event_born_time,
            event.headers.event_corp_id,
            event.headers.event_unified_app_id,
            event.data)
        # put your code here; 可以在这里添加你的业务代码，处理事件订阅的业务逻辑；
        data = event.data
        if event.headers.event_type == 'bpms_instance_change':
            #  todo 修改状态
            data = DingtalkProcessInstanceChangeEvent.from_dict(data)

        elif event.headers.event_type == 'bpms_task_change':
            data = DingtalkProcessInstanceTaskChangeEvent.from_dict(data)

        logger.info(data)
        return dingtalk_stream.AckMessage.STATUS_OK, 'OK'


def dingtalk_stream_client_start():
    AUTH_DINGDING_APP_KEY = env("AUTH_DINGDING_APP_KEY")
    AUTH_DINGDING_APP_SECRET = env("AUTH_DINGDING_APP_SECRET")

    credential = dingtalk_stream.Credential(AUTH_DINGDING_APP_KEY, AUTH_DINGDING_APP_SECRET)
    client = dingtalk_stream.DingTalkStreamClient(credential)
    client.register_all_event_handler(ArcheryDingtalkEventHandler())
    client.start_forever()


if settings.CURRENT_AUDITOR == 'sql.utils.workflow_audit:DingTalkAudit':
    thread = threading.Thread(target=dingtalk_stream_client_start)
    thread.start()


@dataclass
@dataclass_json
class DingtalkProcessInstanceChangeEvent:
    processInstanceId: str
    eventId: str
    resource: str
    createTime: int
    processCode: str
    title: str
    type: str
    staffId: str
    businessType: Optional[str]
    bizCategoryId: Optional[str]
    url: Optional[str]


@dataclass
@dataclass_json
class DingtalkProcessInstanceTaskChangeEvent:
    processInstanceId: str
    eventId: str
    resource: str
    createTime: int
    processCode: str
    title: str
    type: str
    staffId: str
    businessType: Optional[str]
    bizCategoryId: Optional[str]
    activityId: str
    activityName: str
    taskId: int


@superuser_required
def sync_ding_user(request):
    """主动触发同步接口，同时写入schedule每天进行同步"""
    try:
        # 添加schedule并触发同步
        add_sync_ding_user_schedule()
        return JsonResponse({"status": 0, "msg": f"触发同步成功"})
    except Exception as e:
        return JsonResponse({"status": 1, "msg": f"触发同步异常:{e}"})
