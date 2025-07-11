#!/usr/bin/env python
# -*- coding: utf-8 -*-
import json
import logging
import pickle
import re
import time
from datetime import datetime, timedelta
from typing import Optional

import dingtalk_stream
import prettytable
import requests
from asgiref.sync import sync_to_async
from django.core.cache import cache
from django.http import JsonResponse
from django.utils import timezone
from django_q.tasks import async_task
from pydantic import BaseModel

from archery.settings import env
from common.config import SysConfig
from common.utils.const import WorkflowAction, WorkflowStatus, WorkflowType
from common.utils.permission import superuser_required
from sql.models import Users, WorkflowAudit, WorkflowLog, WorkflowAuditDetail, SqlWorkflowContent
from sql.utils.tasks import add_sync_ding_user_schedule, del_schedule, add_sql_schedule

logger = logging.getLogger("default")
DEFAULT_TTL = 60 * 60 * 24


class DingAPIException(Exception):
    pass


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


def get_ding_user_id_by_mobile(mobile):
    token = get_access_token_by_env()
    try:
        url = f"https://oapi.dingtalk.com/user/get_by_mobile?mobile={mobile}&access_token={token}"
        resp = requests.get(url, timeout=3).json()
        if resp.get("errcode") == 0:
            return resp.get("userid")
        else:
            err_msg = f"获取手机号 [{mobile}] 对应钉钉用户ID失败: [{resp.get('errmsg')}]"
            logger.error(err_msg)
            raise DingAPIException(err_msg)
    except Exception as e:
        if isinstance(e, DingAPIException):
            raise e
        logger.error(f"获取手机号 [{mobile}] 对应钉钉用户ID失败: [{e}]")
        raise Exception(f"获取用户 [{mobile}] 信息失败")


def sync_ding_user_id():
    """
    使用工号（username）登陆archery，并且工号对应钉钉系统中字段 "jobnumber"。
    所以可根据钉钉中 jobnumber 查到该用户的 ding_user_id。
    """
    sys_config = SysConfig()
    ding_dept_ids = sys_config.get("ding_dept_ids", "")
    username2ding = sys_config.get("ding_archery_username")
    token = get_access_token_by_env()
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


def sync_ding_user_id_by_mobile():
    """
    用于定时任务 使用 mobile同步 dingding_user_id

    应用启动时自动创建定时任务

    每天运行一次
    :return:
    """
    token = get_access_token_by_env()
    if not token:
        return "未配置或错误的 APP_KEY & APP_SECRET"
    users = Users.objects.filter(mobile__isnull=False)
    if not users or len(users) == 0:
        return "无可用手机号进行同步"
    cnt = 0
    for user in users:
        try:
            ding_id = get_ding_user_id_by_mobile(user.mobile)
            user.ding_user_id = ding_id if ding_id else ''
            logger.info(f"成功为用户 [{user.display}] 同步钉钉ID: [{ding_id}]")
            cnt += 1
        except Exception as e:
            logger.error(f"获取用户 [{user.display}] 钉钉ID 失败, {e}")
    # 批量更新, 减少db操作次数
    batch_size = 1000
    data_tobe_update = [users[i: i + batch_size] for i in range(0, len(users), batch_size)]
    for data in data_tobe_update:
        Users.objects.bulk_update(data, ["ding_user_id"])
    return f"同步数量: {cnt}"


def get_process_code_by_name(process_name) -> Optional[str]:
    """
    通过模板名称获取审批模版 code
    :param process_name: 模板名称
    :return: process_code, 不存在时返回 None
    """
    token = get_access_token_by_env()
    url = f"POST https://oapi.dingtalk.com/topapi/process/get_by_name?access_token={token}"
    data = {'process_name': process_name}
    try:
        resp = requests.post(url, json=data).json()
        if resp.get("errcode") == 0:
            return resp.get("process_code")
        else:
            logger.error(f"获取审批模版 {process_name} 失败: {resp}")
            return None
    except Exception as e:
        logger.error(f"获取审批模版 {process_name} 失败: {e}")
        return None


def delete_process_template(process_code) -> bool:
    """
    删除审批模板
    :param process_code: 审批模板 code
    :return: 是否删除成功， 删除失败则打印日志
    """
    token = get_access_token_by_env()
    url = f"https://oapi.dingtalk.com/topapi/process/delete?access_token={token}"
    data = {"process_code": process_code}
    try:
        resp = requests.post(url, json=data).json()
        if resp.get("errcode") == 0:
            return True
        else:
            logger.error(f"删除审批模版 {process_code} 失败: {resp}")
            return False
    except Exception as e:
        logger.error(f"删除审批模版失败: {e}")
        return False


def create_or_update_process_default_template():
    """
    创建默认的审批模板

    这个模板不包含审批流程, 老版本 API 也不支持

    暂时无用, 等后续支持在 Archer 端选择审批人 和 抄送人 再启用

    :return: 审批模板 code
    """
    default_process_name = '[Archery] 审批'
    try:
        process_code = get_process_code_by_name(default_process_name)
        if not process_code:
            # 新增一个模板
            token = get_access_token_by_env()
            url = f"https://oapi.dingtalk.com/topapi/process/save?access_token={token}"
            data = {
                'saveProcessRequest': {
                    'name': default_process_name,
                    'description': '用于 Archery 的审批表单',
                    'form_component_list': [
                        {
                            "componentType": "DDSelectField",
                            "props": {
                                "options": [
                                    {
                                        "value": "查询权限申请",
                                        "key": "query"
                                    },
                                    {
                                        "value": "SQL上线申请",
                                        "key": "review"
                                    },
                                    {
                                        "value": "数据归档申请",
                                        "key": "archive"
                                    },
                                ],
                                "label": "审批类型",
                                "placeholder": "请选择",
                                "componentId": "DDSelectField_14T8M4EKXAV40",
                                "required": True
                            }
                        },
                        {
                            "component_name": "TextareaField",
                            "props": {
                                "required": True,
                                "placeholder": "详细信息",
                                "label": "详细信息",
                                "id": "TextareaField-J78F056S"
                            }
                        }
                    ]
                }
            }
            resp = requests.post(url, json=data).json()
            if resp.get("errcode") == 0:
                return resp.get("result", {}).get("process_code")
    except Exception as e:
        logger.error(f"创建审批模版失败: {e}")
        raise e


def get_access_token_by_env():
    """
    从环境变量中取出钉钉相关配置，而后获取token
    :return: token
    """
    AUTH_DINGDING_APP_KEY = env("AUTH_DINGDING_APP_KEY")
    AUTH_DINGDING_APP_SECRET = env("AUTH_DINGDING_APP_SECRET")
    token = get_access_token(AUTH_DINGDING_APP_KEY, AUTH_DINGDING_APP_SECRET)
    return token


def get_ding_user_info(username):
    user = Users.objects.get(username=username)
    dingtalk_id = user.ding_user_id
    if not dingtalk_id:
        raise Exception(f"钉钉用户不存在，请联系管理员")
    ding_depts = cache.get(f'ding_depts:{username.lower()}')
    if ding_depts:
        return dingtalk_id, ding_depts
    token = get_access_token_by_env()

    try:
        list_parent_depts_url = f'https://oapi.dingtalk.com/department/list_parent_depts?access_token={token}&userId={dingtalk_id}'
        resp = requests.get(list_parent_depts_url, timeout=3).json()
        if resp.get("errcode") == 0:
            _res = resp.get("department")
            cache.set(f'ding_depts:{username.lower()}', _res[0][0], 86400)
            return dingtalk_id, _res[0][0]
    except Exception as e:
        logger.error(f"获取用户 {username} 钉钉所属部门失败:  {e}")
        raise Exception(f"获取用户 {username} 钉钉所属部门失败, 请联系管理员")


def create_process(process_code, workflow_type, workflow) -> str:
    """
    创建审批实例， 返回审批实例 ID
    """
    data = build_process_instance_data(process_code, workflow, workflow_type)
    token = get_access_token_by_env()

    try:
        url = f'https://oapi.dingtalk.com/topapi/processinstance/create?access_token={token}'
        resp = requests.post(url, json=data, timeout=3).json()
        if resp.get("errcode") == 0:
            return resp.get("process_instance_id")
        else:
            err_msg = resp.get("errmsg")
            request_id = resp.get("request_id")
            logger.error(f'创建钉钉审批流失败: {err_msg}, request_id: {request_id}')
            raise Exception(err_msg)
    except Exception as e:
        logger.error(f"创建审批流失败: {e}")
        raise Exception(f"创建审批流失败，请联系管理员")


def build_process_instance_data(process_code, workflow, workflow_type):
    """
    构建审批实例需要的表单信息
    """
    if workflow_type == WorkflowType.QUERY:
        workflow_title = workflow.title
        create_user = workflow.user_name
        form_component_values = [
            {
                'name': '审批类型',
                'value': WorkflowType.QUERY.label
            },
            {
                'name': '详细信息',
                'value': f'''
基础信息
----------
标题: {workflow_title}
申请人: {create_user}
----------

资源信息
----------
资源组: {workflow.group_name}
实例: {workflow.instance.instance_name}
----------

权限信息
----------
权限级别: {'数据库级别' if workflow.priv_type == 1 else '表级别'}
数据库: {workflow.db_list}
表: {workflow.table_list}
授权时间: {workflow.valid_date}
查询限制数量: {workflow.limit_num}
----------
'''
            },
        ]
    elif workflow_type == WorkflowType.ARCHIVE:
        workflow_title = workflow.title
        create_user = workflow.user_name
        archive_mode = workflow.mode
        archive_content = ''
        if archive_mode == 'file':
            archive_mode_display = '归档到文件'
            archive_content = f'''
归档后是否保留源数据: {'保留' if workflow.no_delete else '删除'}
'''
        elif archive_mode == 'dest':
            archive_mode_display = '归档到其他实例'
            archive_content = f'''
归档后是否保留源数据: {'保留' if workflow.no_delete else '删除'}
目标实例: {workflow.dest_instance.instance_name}
目标数据库: {workflow.dest_db_name}
目标表: {workflow.dest_table_name}
'''
        elif archive_mode == 'purge':
            archive_mode_display = '直接删除'
        form_component_values = [
            {
                'name': '审批类型',
                'value': WorkflowType.ARCHIVE.label
            },
            {
                'name': '详细信息',
                'value': f'''
基础信息
----------
标题: {workflow_title}
申请人: {create_user}
----------

资源信息
----------
资源组: {workflow.group_name}
实例: {workflow.src_instance.instance_name}
----------

归档信息
----------
源数据库: {workflow.src_db_name}
归档表: {workflow.src_table_name}
归档模式: {archive_mode_display}
{archive_content}
归档条件: {workflow.condition}
归档10000行记录后休眠秒数: {workflow.sleep}
----------

'''
            },
        ]
    elif workflow_type == WorkflowType.SQL_REVIEW:
        workflow_title = workflow.workflow_name
        create_user = workflow.engineer
        run_date_str = '-'
        workflow_content = SqlWorkflowContent.objects.get(workflow=workflow)
        review_content = json.loads(workflow_content.review_content)
        tab = prettytable.PrettyTable()
        tab.field_names = ['ID', '审核/执行状态', '审核/执行信息', '当前阶段']
        for ct in review_content:
            err_level = 'pass'
            if ct['errlevel'] == 1:
                err_level = 'warning'
            elif ct['errlevel'] == 2:
                err_level = 'error'
            tab.add_row([ct['id'], err_level, ct['errormessage'], ct['stagestatus']])

        if workflow.run_date_start and workflow.run_date_end:
            run_date_str = workflow.run_date_start.strftime('%Y-%m-%d %H:%M:%S') + '-' + workflow.run_date_end.strftime(
                '%Y-%m-%d %H:%M:%S')
        form_component_values = [
            {
                'name': '审批类型',
                'value': WorkflowType.SQL_REVIEW.label
            },
            {
                'name': '详细信息',
                'value': f'''
基础信息
----------
工单名称: {workflow_title}
申请人: {create_user}
----------

资源信息
----------
资源组: {workflow.group_name}
实例: {workflow.instance.instance_name}
数据库: {workflow.db_name}
可执行时间范围: {run_date_str}
----------

审核信息 
----------
{tab.get_string()}

'''
            }
        ]
    dingtalk_id, dingtalk_dept_id = get_ding_user_info(create_user)
    data = {
        'process_code': process_code,
        'originator_user_id': dingtalk_id,
        'dept_id': dingtalk_dept_id,
        'form_component_values': form_component_values
    }
    return data


def close_process_instance(process_instance_id, ding_user_id, remark):
    """
    终止审批流程, archery 端主动终止
    :param process_instance_id: 实例 id
    :param remark: 终止说明
    :return:
    """
    token = get_access_token_by_env()
    url = f'https://oapi.dingtalk.com/topapi/process/instance/terminate?access_token={token}'
    body = {
        'request': {
            'process_instance_id': process_instance_id,
            'is_system': False,
            'operating_userid': ding_user_id,
            'remark': remark
        }
    }
    try:
        resp = requests.post(url, json=body).json()
        if not resp.get("errcode") == 0:
            logger.error(
                f"终止审批流失败: {resp.get('errmsg', '')}, process_instance_id: {process_instance_id}, request_id: {resp.get('request_id', '')}")
            raise Exception(f"{resp.get('errmsg', '')}")
    except Exception as e:
        logger.error(f"终止审批流失败: {e}")
        raise e


def get_all_visiable_bpms_process(username):
    """
    根据当前用户获取其能查看的所有表单
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

    try:
        rd_data = pickle.dumps(datas)
        cache.set(f"ding_bpms:{user_id}", rd_data, timeout=DEFAULT_TTL)
        for item in datas:
            cache.set(f"ding_bpms:code:{item['process_code']}", item['name'], timeout=DEFAULT_TTL)
    except Exception as re:
        logger.error(f"设置 [钉钉审批表单] redis 缓存失败: {re}")
    return datas


def get_process_instance_detail(process_instance_id):
    try:
        detail = cache.get(f"ding_process_instance_detail:{process_instance_id}")
        if detail:
            return pickle.loads(detail)
    except Exception as e:
        logger.warning(f"无法获取缓存数据，调用 API 获取数据")
    try:
        token = get_access_token_by_env()
        url = f'https://oapi.dingtalk.com/topapi/processinstance/get?access_token={token}'
        body = {'process_instance_id': process_instance_id}
        response = requests.post(url, json=body).json()
        if not response.get("errcode") == 0:
            raise DingAPIException(f"{response.get('errmsg', '')}")
        result = response.get("process_instance")
        if result['status'] in ['COMPLETED', 'TERMINATED', 'CANCELED']:
            # 已经完成/终止的流程, 不会有变动, 放入缓存减少请求次数
            cache.set(f"ding_process_instance_detail:{process_instance_id}", pickle.dumps(result))
        return result

    except Exception as e:
        if isinstance(e, DingAPIException):
            raise e
        logger.error(f"获取审批详情失败: {e}")
        raise Exception(f"获取审批详情失败, 请联系管理员")


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
    """
    sys_config: SysConfig = SysConfig()

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
        _data = event.data
        if event.headers.event_type == 'bpms_instance_change':
            # 修改状态
            _data = DingtalkProcessInstanceChangeEvent(**_data)
            audit, cur_user = await self.get_audit_and_user(_data.processInstanceId, _data.staffId)
            if not audit or not cur_user:
                pass

            if _data.type == 'finish':
                # 事件结束，推动流程
                passed = _data.result == 'agree'
                audit.current_audit = "-1"
                audit.current_status = WorkflowStatus.PASSED if passed else WorkflowStatus.REJECTED
                audit_detail = WorkflowAuditDetail(
                    audit_id=audit.audit_id,
                    audit_user=cur_user.username,
                    audit_status=audit.current_status,
                    audit_time=timezone.now(),
                    remark=_data.result,
                )
                await self.save_audit_info(audit, audit_detail)

            elif _data.type == 'terminate':
                # 记录明细
                audit_detail = await self.get_audit_detail_with_status(audit_id=audit.audit_id)
                if not audit_detail:
                    audit_detail = WorkflowAuditDetail(
                        audit_id=audit.audit_id,
                        audit_user=cur_user.username,
                        audit_status=audit.current_status,
                        audit_time=timezone.now(),
                        remark=f'{cur_user.username} 终止流程',
                    )
                # 主动终止， 记录日志
                audit.current_audit = "-1"
                audit.current_status = WorkflowStatus.ABORTED
                await self.save_audit_info(audit, audit_detail)

                # 记录日志
                audit_log = WorkflowLog(
                    audit_id=audit.audit_id,
                    operation_type=WorkflowAction.ABORT.value,
                    operation_type_desc=WorkflowAction.ABORT.label,
                    operation_info=f'{cur_user.username} 终止流程',
                    operator=cur_user.username,
                    operator_display=cur_user.display if cur_user.display else cur_user.username,
                )
                await self.save_audit_log(audit_log)

        elif event.headers.event_type == 'bpms_task_change':
            _data = DingtalkProcessInstanceTaskChangeEvent(**_data)

            audit, cur_user = await self.get_audit_and_user(_data.processInstanceId, _data.staffId)
            if not audit or not cur_user:
                pass
            logger.info('开始处理事件')
            audit_log = WorkflowLog(
                audit_id=audit.audit_id,
                operation_type=WorkflowAction.SUBMIT.value,
                operation_type_desc=WorkflowAction.SUBMIT.label,
                operation_info='',
                operator=cur_user.username,
                operator_display=cur_user.display if cur_user.display else cur_user.username,
            )
            logger.info('事件审批日志,', audit_log)
            if _data.type == 'start':
                audit_log.operation_info = f'钉钉 ==> {cur_user.display} 开始审批'
            elif _data.type == 'comment':
                # 记录日志
                audit_log.operation_type = WorkflowAction.COMMENT.value
                audit_log.operation_type_desc = WorkflowAction.COMMENT.label
                audit_log.operation_info = f'{cur_user.display} 评论: {_data.content}'
            elif _data.type == 'finish':
                # 结束事件，记录是否成功
                task_result = ''
                if _data.result == 'agree':
                    task_result = '通过'
                    audit_log.operation_type = WorkflowAction.PASS.value
                    audit_log.operation_type_desc = WorkflowAction.PASS.label
                    # 触发定时执行
                    # 在最后一步的时候，审批通过可以设定指令触发定时执行SQL， 但是要严格控制对应的时间信息
                    if _data.remark:
                        await self.try_add_sql_task(audit, cur_user, _data.remark)
                elif _data.result == 'refuse':
                    task_result = '拒绝'
                    audit_log.operation_type = WorkflowAction.REJECT.value
                    audit_log.operation_type_desc = WorkflowAction.REJECT.label
                audit_log.operation_info = f"钉钉 ==> {cur_user.display} 审批{task_result}"
            elif _data.type == 'cancel':
                # 取消审批日志
                audit_log.operation_type = WorkflowAction.ABORT.value
                audit_log.operation_type_desc = WorkflowAction.ABORT.label
                audit_log.operation_info = f'{cur_user.display} 取消审批'
            await self.save_audit_log(audit_log)
        return dingtalk_stream.AckMessage.STATUS_OK, 'OK'

    @sync_to_async
    def get_audit_and_user(self, process_instance_id, staff_id):
        audit = WorkflowAudit.objects.filter(audit_auth_groups=process_instance_id)
        if not audit or len(audit) == 0:
            logger.warning(f"事件信息不属于当前环境")
            return None, None
        cur_user = Users.objects.filter(ding_user_id=staff_id)
        if not cur_user:
            return audit.first(), None
        return audit.first(), cur_user.first()

    @sync_to_async()
    def get_audit_detail_with_status(self, audit_id):
        return WorkflowAuditDetail.objects.get(audit_id=audit_id)

    @sync_to_async
    def save_audit_log(self, audit_log: WorkflowAuditDetail):
        audit_log.save()

    @sync_to_async
    def try_add_sql_task(self, audit, cur_user, remark):
        """
        尝试添加定时执行任务
        """
        _rmk = remark.split('\n')
        for _r in _rmk:
            run_date = self.parse_execute_command(_r)
            if run_date:
                add_sql_schedule(f"sqlreview-timing-{audit.workflow_id}", run_date, audit.workflow_id)
                timing_log = WorkflowLog(
                    audit_id=audit.audit_id,
                    operation_type=4,
                    operation_type_desc="定时执行",
                    operation_info="钉钉 ==> 定时执行时间：{}".format(run_date.strftime("%Y-%m-%d %H:%M:%S")),
                    operator=cur_user.username,
                    operator_display=cur_user.display if cur_user.display else cur_user.username,
                )
                timing_log.save()
                break

    @sync_to_async
    def save_audit_info(self, audit: WorkflowAudit, audit_detail: WorkflowAuditDetail):
        audit.save()
        audit_detail.save()
        workflow = audit.get_workflow()
        # 处理工单状态
        if workflow.workflow_type == WorkflowType.QUERY:
            from sql.query_privileges import _query_apply_audit_call_back
            _query_apply_audit_call_back(
                audit.workflow_id,
                audit.current_status,
            )
        elif workflow.workflow_type == WorkflowType.SQL_REVIEW:
            if audit.current_status == WorkflowStatus.PASSED:
                workflow.status = "workflow_timingtask"
                workflow.save(update_fields=["status"])
                self.try_notify_pass(audit, audit_detail)
            elif audit.current_status in [
                WorkflowStatus.ABORTED,
                WorkflowStatus.REJECTED,
            ]:
                if workflow.status == "workflow_timingtask":
                    del_schedule(f"sqlreview-timing-{workflow.id}")
                    # 将流程状态修改为人工终止流程
                workflow.status = "workflow_abort"
                workflow.save(update_fields=["status"])
        elif workflow.workflow_type == WorkflowType.ARCHIVE:
            workflow.status = audit.current_status
            if audit.current_status == WorkflowStatus.PASSED:
                workflow.state = True

            else:
                workflow.state = False
            workflow.save(update_fields=["status", "state"])

    @sync_to_async
    def save_audit_detail(self, audit_detail: WorkflowAuditDetail):
        audit_detail.save()

    def parse_time(self, time_str):
        """解析时间字符串，支持绝对时间、相对时间和 'now'"""
        # 匹配绝对时间格式：YYYY-MM-DD HH:MM:SS
        absolute_match = re.match(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})$', time_str)
        if absolute_match:
            _target_date = datetime.strptime(absolute_match.group(1), "%Y-%m-%d %H:%M:%S")
            # 时间小于当前时间, 则直接取当前时间 +30s
            if _target_date < datetime.now():
                _target_date = timedelta(seconds=30) + _target_date
            return _target_date

        # 匹配 'now'
        if time_str == 'now':
            # +30s, 防止 DB 卡顿导致的任务先于 db 提交执行
            return datetime.now() + timedelta(seconds=30)

        # 匹配相对时间格式：如 30s, 5m, 1h, 1h20m, 1h30m2s,1h30m20s 等
        relative_pattern = r'((?P<weeks>\d+)w)?((?P<days>\d+)d)?((?P<hours>\d+)h)?((?P<minutes>\d+)m)?((?P<seconds>\d+)s)?((?P<months>\d+)M)?'
        relative_match = re.fullmatch(relative_pattern, time_str)
        if relative_match:
            time_dict = relative_match.groupdict()
            total_seconds = 0
            for unit, value in time_dict.items():
                if value:
                    # 转换为秒（1个月=30天，1周=7天）
                    if unit == 'weeks':
                        total_seconds += int(value) * 7 * 24 * 3600
                    elif unit == 'days':
                        total_seconds += int(value) * 24 * 3600
                    elif unit == 'hours':
                        total_seconds += int(value) * 3600
                    elif unit == 'minutes':
                        total_seconds += int(value) * 60
                    elif unit == 'seconds':
                        total_seconds += int(value)
                    elif unit == 'months':
                        total_seconds += int(value) * 30 * 24 * 3600
            return datetime.now() + timedelta(seconds=total_seconds)
        logger.error(f"无法解析 execute 指令后的时间: {time_str}")
        return None

    def parse_execute_command(self, command):
        """解析 /execute 指令"""
        match = re.match(r'^/execute\s+(.+)$', command)
        if not match:
            logger.error(f"无法解析 execute 指令: {command}")
            return None

        time_str = match.group(1)
        return self.parse_time(time_str)

    def try_notify_pass(self, audit, audit_detail):
        # 通知
        is_notified = (
            "Pass" in self.sys_config.get("notify_phase_control").split(",")
            if self.sys_config.get("notify_phase_control")
            else True
        )
        if is_notified:
            from sql.notify import notify_for_audit
            async_task(
                notify_for_audit,
                workflow_audit=audit,
                workflow_audit_detail=audit_detail,
                timeout=60,
                task_name=f"sqlreview-pass-{audit.workflow_id}",
            )


def dingtalk_stream_client_start():
    AUTH_DINGDING_APP_KEY = env("AUTH_DINGDING_APP_KEY")
    AUTH_DINGDING_APP_SECRET = env("AUTH_DINGDING_APP_SECRET")

    credential = dingtalk_stream.Credential(AUTH_DINGDING_APP_KEY, AUTH_DINGDING_APP_SECRET)
    client = dingtalk_stream.DingTalkStreamClient(credential)
    client.register_all_event_handler(ArcheryDingtalkEventHandler())
    client.start_forever()


class DingtalkProcessInstanceChangeEvent(BaseModel):
    processInstanceId: str
    eventId: str
    resource: str
    createTime: int
    processCode: str
    title: str
    type: str
    staffId: str
    finishTime: Optional[int] = None
    result: Optional[str] = None
    businessId: Optional[str] = None
    businessType: Optional[str] = None
    bizCategoryId: Optional[str] = None
    remark: Optional[str] = None
    content: Optional[str] = None
    url: Optional[str] = None


class DingtalkProcessInstanceTaskChangeEvent(BaseModel):
    processInstanceId: str
    eventId: str
    resource: str
    createTime: int
    processCode: str
    title: str
    type: str
    result: Optional[str] = None
    staffId: Optional[str] = None
    businessId: Optional[str] = None
    businessType: Optional[str] = None
    bizCategoryId: Optional[str] = None
    activityId: Optional[str] = None
    activityName: Optional[str] = None
    finishTime: Optional[int] = None
    remark: Optional[str] = None
    content: Optional[str] = None
    taskId: Optional[int] = None

@superuser_required
def trigger_sync_ding_user(request):
    async_task(
        sync_ding_user_id_by_mobile,
        timeout=60,
        task_name=f"sync-ding-user-by-mobile-{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}",
    )

@superuser_required
def sync_ding_user(request):
    """主动触发同步接口，同时写入schedule每天进行同步"""
    try:
        # 添加schedule并触发同步
        add_sync_ding_user_schedule()
        return JsonResponse({"status": 0, "msg": f"触发同步成功"})
    except Exception as e:
        return JsonResponse({"status": 1, "msg": f"触发同步异常:{e}"})
