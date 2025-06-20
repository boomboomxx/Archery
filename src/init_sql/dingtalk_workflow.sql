alter table workflow_audit_setting
    add column channel TINYINT(4) not null comment '审批渠道: 1 - 本地, 2 - 钉钉';
alter table workflow_audit_setting
    add column channel_process_code varchar(255) not null comment '三方审批工作流id';