---
name: excel
description: 处理 Excel（.xlsx）文件：先看表结构，再按列做字段脱敏（手机号、姓名、身份证、邮箱、银行卡、哈希、整列删除）
---

# Excel 字段脱敏

用户上传 .xlsx 并要求「脱敏 / 打码 / 隐藏敏感字段」时走这条链路。

## 步骤

1. **先看表结构**
   `run_skill_script(skill="excel", script="inspect", params={"file_id": "<会话文件清单里的 file_id>"})`
   返回每个 sheet 的列名、样本值与行数。

2. **照样本值认列，不要照用户的原话认**
   用户说的「手机号」在表里可能叫「联系电话」，只看列名猜不出来，样本值的格式能认出来。

3. **套规则**
   `run_skill_script(skill="excel", script="desensitize", params={...})`：

   ```json
   {
     "file_id": "<会话文件清单里的 file_id>",
     "sheet": "Sheet1",
     "header_row": 1,
     "ops": [
       {"column": "联系电话", "rule": "phone_middle4"},
       {"column": "姓名", "rule": "name_keep_first"},
       {"column": "身份证号", "rule": "drop"}
     ],
     "salt": "可选，只有 hash 规则用得上"
   }
   ```

   产物默认叫「已脱敏.xlsx」，可用 `out_name` 改；原件不会被改动。

## rule 可选值

| rule | 效果 | 例子 |
| --- | --- | --- |
| `phone_middle4` | 11 位手机号打码中间 4 位 | 13812341234 → 138****1234 |
| `name_keep_first` | 姓名只留姓 | 张三 → 张* |
| `idcard_keep_ends` | 身份证留前 6 后 4 | 110101199001011234 → 110101********1234 |
| `email_mask` | 邮箱只留首字母 | zhangsan@qq.com → z***@qq.com |
| `bankcard_keep_last4` | 银行卡留末 4 位 | 6222021234567890 → ************7890 |
| `hash` | 加盐哈希前 16 位（同值同结果，不可反查） | 张三 → 8f14e45fceea167a |
| `drop` | 整列删除 | |

值跟规则对不上时原样保留：手机号列里混着的「无」、座位号不会被硬改。

## 完成后

产物已经挂在会话文件里（工具返回的 `artifacts` 就是），用户可以直接下载。
回答里说清楚：处理了哪个 sheet、哪几列、用了什么规则、产物叫什么名字。
