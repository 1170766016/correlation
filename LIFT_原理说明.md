# Lift（提升度）计算原理说明

## 一、核心概念

### 1.1 什么是 Lift（提升度）

Lift（提升度）衡量的是：**某个特征取值在 Fail 产品中的占比，相对于其在整体基准（Pass + Fail）中的占比的倍数**。

```
Lift = P(Fail | 特征取值) / P(基准 | 特征取值)
     = (该取值的 Fail 数 / 总 Fail 数) / (该取值的基准行数 / 总基准行数)
```

- **Lift = 1**：该取值在 Fail 中和在整体中比例一致，无聚集
- **Lift > 1**：该取值在 Fail 中的比例高于在整体中的比例，存在异常聚集
- **Lift < 1**：该取值在 Fail 中的比例低于在整体中的比例

### 1.2 基准池（Base Pool）定义

基准池由两部分组成：

| 组成部分 | 说明 |
|---------|------|
| 全部 Pass 产品 | 所有通过终检的产品 |
| 指定 Failed_Station 的 Fail 产品 | 仅包含当前筛选工站的不良品 |

> 即：`df_base = Pass ∪ Fail(指定工站)`

不包含其他工站的 Fail 产品，避免它们稀释 Lift 信号。

---

## 二、离散制程因素 Lift 计算

### 2.1 数据准备

1. 筛选日期范围、Failed_Station
2. 构造 `df_base`（基准池）和 `df_fail`（Fail 池）
3. 确定参与分析的特征列（MC_ID、Cavity_ID、Vendor、lot_ID、Nozzle、Socket 等）

### 2.2 特征列过滤

| 过滤条件 | 说明 |
|---------|------|
| 唯一值 > `max_unique` | 超过 30% 行数的特征（如序列号）跳过 |
| 唯一值 ≤ 1 | 常量列跳过 |
| 时间列（`_End_Time` / `_Start_Time`） | 跳过，由单独的时间类分析处理 |

### 2.3 核心计算流程

```
对每个特征列 col：
  1. 计算 base 和 fail 两侧的 value_counts
  2. 对齐索引（只考虑 fail 中出现的取值）
  3. 对每个取值 v：
     fail_cnt = fail 中 v 的出现次数
     base_cnt = base 中 v 的出现次数
     p_fail = fail_cnt / n_fail        # Fail 内占比
     p_base = base_cnt / n_base        # 基准占比
     lift = p_fail / p_base
     
     若 lift > 1.0 且 fail_cnt ≥ min_count(3)：
       加入 lift_results（用于 Lift 排行榜）
     
     无条件加入 ratio_results（用于 Fail内占比排行榜）
```

### 2.4 输出指标

| 指标 | 公式 | 含义 |
|------|------|------|
| `lift` | `p_fail / p_base` | 提升度，越大越异常 |
| `fail_ratio` | `p_fail` | 该取值在 Fail 样本中的占比 |
| `p_fail` | `fail_cnt / n_fail` | Fail 集中度 |
| `p_base` | `base_cnt / n_base` | 基准占比 |
| `fail_count` | `fail_cnt` | Fail 中出现次数 |
| `base_count` | `base_cnt` | 基准中出现次数 |
| `composite_score` | `lift_weight × lift + (1-lift_weight) × (fail_ratio × 100)` | 综合评分 |

### 2.5 TOP 10 选取策略

```
对 lift_results 按 composite_score 降序排列
每个特征列只保留 composite_score 最高的一个取值
取前 10 个特征
```

---

## 三、时间日期类 Lift 计算

### 3.1 时间列识别

匹配以下后缀的列名（不区分大小写）：
- `_End_Time` — 结束时间
- `_Start_Time` — 开始时间
- `_datetime` — 日期时间

> `_Staging_time`（持续秒数）**不参与**时间类分析。

### 3.2 小时特征提取

```
对每个时间列 col：
  1. pd.to_datetime(parsed) 解析时间戳
  2. 有效解析率 < 30% 则跳过
  3. 创建新列 {col}_Hour = parsed.dt.hour (0-23)
```

时间格式兼容：
- `2026-04-13 13:24:45`（横线分隔）
- `2026/04/13 13:42:51`（斜线分隔）

### 3.3 小时 Lift 计算

与离散制程因素完全相同的 `compute_lift` 逻辑，只是输入特征列变为 `_Hour` 列：

```
对每个 _Hour 列：
  lift = P(Fail | 该小时) / P(基准 | 该小时)
```

### 3.4 小时 Fail 内占比计算

```
fail_ratio = 该小时的 Fail 数 / 总 Fail 数
```

同一套 `ratio_results` 输出，只是过滤阈值从 20% 降至 **5%**（小时分散在 24 个值，天然占比低）。

---

## 四、特殊处理：Fail 内唯一值 = 1

### 触发条件

```
fail_nunique == 1 且 base_nunique > 1
```

即：该特征在 Fail 中全部是同一个取值，但在基准中有多个取值。

### 处理方式

- **不参与 Lift 计算**（因为 `p_base` 分母唯一，Lift 无意义）
- 单独归入 `fail_one_results`，在页面底部「⚠️ Fail 内唯一值=1 的特征」区域展示

---

## 五、数据流概览

```
原始数据
    │
    ├─ 日期筛选 + Failed_Station 筛选
    │
    ├─ df_base（基准池 = Pass ∪ 指定工站 Fail）
    └─ df_fail（Fail 池）
         │
         ├─ classify_columns() → time_cols, discrete_cols
         │
         ├─ discrete_cols → compute_lift()
         │   ├─ lift_results（Lift > 1.0）
         │   ├─ ratio_results（全部）
         │   └─ fail_one_results（唯一值=1）
         │
         └─ time_cols → extract_hour_features() → _Hour 列 → compute_lift()
             ├─ hour_lift_results（小时 Lift > 1.0）
             └─ hour_ratio_results（全部，> 5% 展示）
```

---

## 六、指标卡展示

| Tab | 数据源 | 排序依据 |
|-----|--------|---------|
| 📈 共性聚集度排行榜 | `lift_results` → TOP 10 | `composite_score` 降序 |
| 📉 Fail内占比排行榜 | `ratio_results` (fail_ratio > 20%) → TOP 10 | `fail_ratio` 降序 |
| 🔍 单因子对比 | `top10` 中前 5 个特征 | 按 rank |
| ⏰ 小时 Lift 排行 | `hour_top10` | `composite_score` 降序 |
| ⏰ 小时 Fail 占比排行 | `hour_ratio_results` (fail_ratio > 5%) → TOP 10 | `fail_ratio` 降序 |
