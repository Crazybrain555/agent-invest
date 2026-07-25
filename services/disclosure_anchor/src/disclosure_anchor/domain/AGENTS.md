# domain — 纯领域层（零 IO、零框架依赖）

```text
entities/core.py           Company/Security/Document/ProcessingRun/DocumentUnit/
                           SourceAccess/SourceCheckpoint/TrackedCompany/CompanyIdentifier/OutboxEvent
entities/outbox_events.py  事件工厂（唯一合法的事件构造点：统一 event_kind/change_kind/
                           subject_kind/subject_ref/occurred_at；禁止在 use case 里手拼 OutboxEvent）
ids.py                     new_id(prefix) + 类型化 helper；ULID 毫秒级时间有序、同毫秒非严格单调，
                           排序一律用显式键（created_at+id / order_index+asset_id / seq）
errors.py                  typed 异常层级；Parser* 按 deadline/local/task/overload/cancel/
                           version_probe/output_contract/unknown 分型并决定 retry/control 映射
value_objects/             ReportPeriod（regex ^\d{4}(A|Q[1-4])$；半年报=YYYYQ2）、
                           filing_type 词表、provider 白名单、QuarantineReason 闭集
```

规范：新枚举值一律走契约升版，禁止自由字符串；错误要么可恢复要么带上下文 re-raise，
不吞异常。词表/枚举的业务含义以 service-purpose §5 为权威。
