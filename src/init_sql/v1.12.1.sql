alter table workflow_audit_setting
    add column channel TINYINT(4) not null comment '审批渠道: 1 - 本地, 2 - 钉钉';

alter table workflow_audit_setting
    add column channel_process_code varchar(255) not null comment '三方审批工作流id';

alter table workflow_audit modify column current_audit varchar (55) not null comment '当前审批权限组';


alter table sql_workflow
    add column channel_audit_instance_id varchar(255) default null comment '渠道审批流 ID';
alter table sql_workflow modify column audit_auth_groups varchar (255) default null comment '审批权限组列表';

alter table resource_group
    add column ding_webhook_sec varchar(255) default null comment '钉钉webhook sec 加签' after ding_webhook;

alter table archive_config
    add column channel_audit_instance_id varchar(255) default null comment '渠道审批流 ID';

alter table query_privileges_apply
    add column channel_audit_instance_id varchar(255) default null comment '渠道审批流 ID';

alter table sql_users
    add column mobile varchar(50) default null comment '手机号' after display;