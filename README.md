# 考勤跨夜班与加班分段核算

面向制造企业薪酬核算员，处理采信、判定、核算、复核与归档。

## 启动

```bash
docker compose up --build -d
```

访问 http://localhost:18252 。初次启动自动运行脏样例，可切换 clean.csv 与 legacy.csv。顶部“运行批次”会重新计算，同源输入复用批次。

## 业务模块

- 打卡采信：`/m1_ingest`、`/api/m1_ingest`
- 班次日历：`/m2_mapping`、`/api/m2_mapping`
- 跨夜配对：`/m3_versions`、`/api/m3_versions`
- 缺卡复核：`/m4_segments`、`/api/m4_segments`
- 工时切段：`/m5_calculate`、`/api/m5_calculate`
- 薪资对账：`/m6_reconcile`、`/api/m6_reconcile`
- 周期归档：`/m7_archive`、`/api/m7_archive`
- 期间分摊封存：`/periods`、`/api/periods`

## 跨期班次裁决

跨月夜班（月末22点至次月6点）**不整班挂起始月，也不为赶截止日只结前半段**，而是按法定日历日在午夜切分为两个期间片段：片段以同一 `shift_id` 串联、各自进入所属工资期间独立封存，原始打卡、片段、倍率与应付逐项对应。

- 未封存期间收到补卡：连同差额重新核对，内容变化才出新快照版本；
- 已封存期间收到补卡：封存快照与校验值绝不改写，差额生成调整（带幂等指纹），落到下一个未封存期间，同一补卡重跑不重复生成调整；
- 任一片段缺失或对账不平即产生发布阻断，封存被拒绝；
- 重算与批次写入在同一事务，失败整体回滚、恢复旧版；
- 页面与导出读取同一份存储快照与 `body_hash`，原始字节与逐条打卡在 `period_batches`/`period_punches` 永久留痕。

## 核心规则

跨夜按班次归属日配对，休息区间先扣除，再按法定日期和工时阈值切分倍率。完整数值、边界和异常定义见 `docs/DATA-DICTIONARY.md`。

## 验证

```bash
docker compose exec -T app pytest -q
docker compose config --quiet
```

原始样例在 `sample_data/`。测试在 `tests/`。封存文件是单份CSV，含运行清单、计算证据、隔离原因与对账。复核需提交当前版本，陈旧复核返回409。

## 数据与限制

SQLite保存在records命名卷，普通重启与构建不会清除。确认可丢弃本项目数据后，运行 `docker compose down -v` 再启动可重置。单文件上限2MB；不接入外部业务系统；历史规则由本项目样例定义，不代表实际行业标准。
