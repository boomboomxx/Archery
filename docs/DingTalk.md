# 钉钉审批流程配置说明

----
## 基础配置
1. 创建钉钉企业内部应用 [参考文档](https://open.dingtalk.com/document/orgapp/application-types)
2. 配置 `.env` 文件
    ```
    # 指定 审批流使用 Dingtalk
    CURRENT_AUDITOR="sql.utils.workflow_audit:DingTalkAudit"
    # 配置钉钉相关密钥, 该密钥信息不会被 Ding_to_person 覆盖, 如果需要使用 ding_2_person, 这里不配置就可以
    AUTH_DINGDING_APP_KEY="<your_app_key>"
    AUTH_DINGDING_APP_SECRET="<your_app_secret>"
   ```
3. 同步钉钉 ID
4. 创建审批表单
   * 名字为: **[Archery] 审批**
   * 表单组件为 
     * 单选组件, 名称一致, 配置3个类型 ![字段1.png](ding_workflow_form_field_1.png)
     * 多行文本组件, 名称一致 ![字段2](ding_workflow_form_field_2.png)
   * 流程配置自己自定义即可
5. 在审批配置中选择对应的审批模板即可

---
### 审批后如何执行
在最后一层审批的时候，填写说明信息，以使用指令
![img.png](dingtalk_workflow_audit_pass_info.png)

支持指令:
`/execute`

支持参数:
* 绝对时间: 2025-01-01 12:00:00
* 相对时间，支持的格式为: `now|h|m|s|w|d|M`
  * `now`: 立即执行, 会在当前时间下 +30s 后执行
  * `s`: 延迟 n 秒后执行
  * `m`: 延迟 n 分钟后执行
  * `h`: 延迟 n 小时后执行
  * `d`: 延迟 n 天后执行
  * `w`: 延迟 n 周后执行
  * `M`: 延迟 n 月执行



#### 栗子🌰
所有参数可以任意指定, 相对时间以累加的形式生成最终时间 , <font style="color: red">所有参数都只能使用一次</font>


1. 使用绝对时间: `2025-01-01 12:00:00`, 如果绝对时间小于当前时间， 则在当前时间基础上 +30s 后执行
2. 使用相对时间，假设基准时间为 `2025-01-01 12:00:00`, 则执行时间对应为:
   * `/execute 30s` ==> `2025-01-01 12:00:30`
   * `/execute 1m` ==> `2025-01-01 12:01:00`
   * `/execute 1h` ==> `2025-01-01 13:00:00`
   * `/execute 1d` ==> `2025-01-02 12:00:00`
   * `/execute 1w` ==> `2025-01-08 12:00:00`
   * `/execute 1h20m` ==> `2025-01-01 13:20:00`
   * `/execute 1d20m20s` ==> `2025-01-02 12:20:20`
   * `/execute 1w20m20s` ==> `2025-01-08 12:20:20`
   * `/execute 1w2w3w`  ==> Unsupported
