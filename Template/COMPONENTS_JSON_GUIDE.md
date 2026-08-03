# components JSON 编写指南

本文档描述 `build_gil_components.py` 当前实际接受的 components JSON 格式。

**解析和编码不依赖任何固定结构体模板。** 每次构建都会从本次上传的 GIA/GIL 或对应 `.structs.json` 获取结构体定义，再按结构体 ID、字段顺序、嵌套关系和字典元数据递归校验 components。本文中的具体结构体仅用于解释格式，不能作为其他文件的 schema。

适用流程：

```text
地图 GIL/GIA
  → 导出 structs.json
  → 编写 components JSON
  → build_gil_components.py
  → 新 GIL
```

本文档中的“结构体定义”均指从目标地图动态导出的 `.structs.json`。生成组件前，必须以目标地图的导出结果为准，不要猜测结构体名称、字段名称或结构体 ID。

当前解析结果还会提供：

| 字段 | 说明 |
| --- | --- |
| `source_format` | `gia` 或 `gil` |
| `extraction_mode` | 实际采用的动态提取方式 |
| `layout_source` | 字段布局来自 GIL schema、GIA schema 或 GIA inline layout |
| `confidence` | `verified` 或经过两种布局互证的 `cross_checked` |

如果上传的 GIA 不包含高级结构体定义，解析器会明确失败，不会根据 Story 等旧模板伪造结构体。

## 1. 最小可用格式

推荐始终使用以下顶层对象格式：

```json
{
  "components": [
    {
      "name": "DemoComponent",
      "variables": []
    }
  ]
}
```

解析器也兼容直接使用组件数组，但不建议让 LLM 生成这种形式：

```json
[
  {
    "name": "DemoComponent",
    "variables": []
  }
]
```

使用统一的对象格式能减少生成结果的分支，并允许按需增加 `id_policy`。

## 2. 构建命令

```powershell
python .\scripts\gil_workflow\build_gil_components.py `
  .\地图.gil `
  .\components.json `
  .\地图_新元件.gil `
  --overwrite
```

执行成功后会生成：

| 文件 | 作用 |
| --- | --- |
| `地图_新元件.gil` | 写入新元件后的 GIL |
| `地图_新元件.summary.json` | 新增元件、变量及 warning 摘要 |
| `地图_新元件.structs.json` | 从输入 GIL 导出的结构体定义 |
| `地图_新元件.workflow.json` | 实际传给底层生成器的工作流规格 |

`--overwrite` 只允许覆盖输出 GIL。未提供时，如果输出文件已存在，构建会失败。

## 3. 顶层字段

| 字段 | JSON 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `components` | array | 是 | 要创建的元件列表 |
| `id_policy` | object | 否 | 自定义 ID 分配起点；一般不要填写 |

### 3.1 `id_policy`

仅在明确知道目标地图 ID 分配策略时使用：

```json
{
  "id_policy": {
    "component_definition_start": 1077940000,
    "component_index_start": 1077950000,
    "value_ref_start": 1075000000
  },
  "components": []
}
```

| 字段 | 作用 |
| --- | --- |
| `component_definition_start` | 新元件定义 ID 起点，写入 `root.f4` |
| `component_index_start` | 新元件索引 ID 起点，写入 `root.f8` |
| `value_ref_start` | 复杂值内部引用 ID 起点 |

生成器会扫描输入 GIL 中已经使用的整数 ID，并从指定起点向后寻找可用 ID。因此，不需要让 LLM 生成这些 ID。

## 4. 元件和变量

### 4.1 元件格式

```json
{
  "name": "元件名称",
  "variables": [
    {
      "name": "变量名称",
      "type": "str",
      "value": "变量值"
    }
  ]
}
```

| 字段 | JSON 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `name` | string | 是 | 元件名称 |
| `variables` | array | 否 | 局部变量列表；省略时等同于空数组 |

变量顺序按 `variables` 数组顺序写入。

### 4.2 变量通用格式

```json
{
  "name": "变量名称",
  "type": "str",
  "value": "文本"
}
```

| 字段 | JSON 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `name` | string | 是 | 变量名称 |
| `type` | string | 二选一 | 推荐使用的变量类型名称 |
| `type_code` | integer | 二选一 | 变量类型码 |
| `value` | 任意 | 否 | 变量值；省略时按对应类型写入默认值 |

`type` 与 `type_code` 至少提供一个。如果两者同时存在，解析器优先使用 `type_code`。为避免名称与类型码冲突，推荐只生成 `type`。

变量名可以是中文、英文或数字字符串，例如 `"对话"`、`"title"`、`"1"`。

## 5. 支持的类型

### 5.1 已支持值写入的类型

| `type` | `type_code` | `value` 格式 | 示例 |
| --- | ---: | --- | --- |
| `str` | 6 | string | `"文本"` |
| `str_list` | 11 | string array | `["甲", "乙"]` |
| `int` | 3 | integer | `3` |
| `int_list` | 8 | integer array | `[1, 2]` |
| `float` | 5 | number | `1.5` |
| `float_list` | 10 | number array | `[0.5, 1.0]` |
| `bool` | 4 | boolean | `true` |
| `bool_list` | 9 | boolean array | `[true, false]` |
| `camp` | 17 | integer | `1` |
| `camp_list` | 24 | integer array | `[1, 2]` |
| `vector` | 12 | `{x,y,z}` 或三元素数组 | `{"x": 1, "y": 2, "z": 3}` |
| `vector_list` | 15 | vector array | `[[1, 2, 3], [4, 5, 6]]` |
| `struct` | 25 | object | 见“结构体变量” |
| `struct_list` | 26 | object array | 见“结构体列表” |
| `dict` | 27 | dictionary object | 见“字典变量” |

类型别名虽然也能被解析，例如 `string`、`integer`、`boolean`、`vec3`，但 LLM 应只使用表中的标准 `type` 名称。

### 5.2 仅默认值稳妥的类型

以下类型可以创建变量，但当前实现尚未确认非默认值的完整编码语义：

| `type` | `type_code` | 推荐默认值 |
| --- | ---: | --- |
| `object` | 1 | `null` |
| `GUID` | 2 | `""` 或 `null` |
| `GUID_list` | 7 | `[]` |
| `object_list` | 13 | `[]` |
| `configurationID` | 20 | `null` |
| `componentID` | 21 | `null` |
| `configurationID_list` | 22 | `[]` |
| `componentID_list` | 23 | `[]` |

对这些类型写入非默认值时，生成器可能保留模板默认编码，并在 `.summary.json` 中产生 warning。涉及这些类型时，必须在编辑器中实测。

## 6. 基础类型示例

```json
{
  "components": [
    {
      "name": "基础类型示例",
      "variables": [
        {
          "name": "标题",
          "type": "str",
          "value": "测试文本"
        },
        {
          "name": "序号",
          "type": "int",
          "value": 3
        },
        {
          "name": "速度",
          "type": "float",
          "value": 1.5
        },
        {
          "name": "启用",
          "type": "bool",
          "value": true
        },
        {
          "name": "标签",
          "type": "str_list",
          "value": ["主线", "测试"]
        },
        {
          "name": "坐标",
          "type": "vector",
          "value": {
            "x": 1.0,
            "y": 2.0,
            "z": 3.0
          }
        }
      ]
    }
  ]
}
```

必须保持 JSON 原生类型：

- 整数写成 `3`，不要写成 `"3"`。
- 布尔值写成 `true` / `false`，不要写成 `"true"` / `"false"`。
- 列表必须使用 `[]`。
- 对象必须使用 `{}`。

## 7. 结构体变量

### 7.1 先读取 `.structs.json`

假设导出的结构体定义包含：

```json
{
  "structs_by_name": {
    "基础复杂文本结构体": 1077936129,
    "运镜结构体": 1077936130,
    "对话": 1077936131,
    "基础跳转对话结构体": 1077936132
  }
}
```

结构体变量推荐通过名称引用：

```json
{
  "name": "对话",
  "type": "struct",
  "struct": "对话",
  "value": {}
}
```

也可以使用 ID：

```json
{
  "name": "对话",
  "type": "struct",
  "struct_id": 1077936131,
  "value": {}
}
```

推荐使用 `struct` 名称，因为它更容易检查。结构体名称必须与 `.structs.json` 的 `structs_by_name` 完全一致。

### 7.2 结构体字段

结构体的 `value` 是一个对象。对象键必须对应该结构体的 `fields[].name`。

例如：

```json
{
  "id": 1077936130,
  "name": "运镜结构体",
  "fields": [
    {"name": "路径索引", "type": "int"},
    {"name": "运动时间（=等待切换时间）", "type": "float"}
  ]
}
```

对应值可以写成：

```json
{
  "路径索引": 1,
  "运动时间（=等待切换时间）": 2.5
}
```

解析规则：

- 缺失字段会按该字段类型写入默认值。
- 多余字段不会写入 GIL，因为生成器只遍历 `.structs.json` 中定义的字段。
- 字段名大小写、空格、括号和全角符号都必须完全匹配。
- 不要把字段类型再次写进 `value`；字段类型来自 `.structs.json`。

### 7.3 嵌套结构体的类型解析

如果 `.structs.json` 已在字段中提供以下信息，生成器会自动解析嵌套结构体类型：

```json
{
  "name": "对话列表",
  "type": "struct_list",
  "element_struct_id": 1077936129
}
```

```json
{
  "name": "跳转对话",
  "type": "struct",
  "struct_id": 1077936132
}
```

这种情况下不需要 `__struct_types__`。

只有当导出的字段缺少 `element_struct_id` / `struct_id`，或者需要显式覆盖嵌套类型时，才在父结构体的 `value` 中加入：

```json
{
  "__struct_types__": {
    "对话列表": "基础复杂文本结构体",
    "跳转对话": "基础跳转对话结构体"
  }
}
```

`__struct_types__` 是生成器元数据，不会成为游戏内结构体字段。

## 8. 结构体列表示例

顶层变量为结构体列表时：

```json
{
  "name": "节点列表",
  "type": "struct_list",
  "struct": "基础复杂文本结构体",
  "value": [
    {
      "等待显示时间": 0.0,
      "标题": "旁白",
      "副标题": "",
      "内容": ["第一句话"],
      "内容显示时间（若需）": []
    },
    {
      "等待显示时间": 0.5,
      "标题": "玩家",
      "副标题": "",
      "内容": ["第二句话"],
      "内容显示时间（若需）": []
    }
  ]
}
```

`struct_list.value` 必须是对象数组。解析器也会把单个对象兼容为单元素列表，但不要依赖该兼容行为。

## 9. 字典变量

### 9.1 推荐格式

顶层字典变量应把字典类型和条目直接写在变量对象中：

```json
{
  "name": "节点图事件触发",
  "type": "dict",
  "key_type": "int",
  "value_type": "str_list",
  "entries": [
    {
      "key": 1,
      "value": ["事件A", "事件B"]
    }
  ]
}
```

| 字段 | JSON 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `key_type` / `key_type_code` | string / integer | 否 | 键类型；缺失时默认为 `str` |
| `value_type` / `value_type_code` | string / integer | 否 | 值类型；缺失时默认为 `str` |
| `value_struct_id` | integer 或结构体名称 | 条件必填 | 值是 `struct` / `struct_list` 时指定结构体 |
| `entries` | array | 否 | 字典条目；缺失时为空字典 |
| `entries[].key` | 对应键类型 | 是 | 字典键 |
| `entries[].value` | 对应值类型 | 是 | 字典值 |

顶层平铺格式是当前最稳妥的写法：生成器选择字典变量模板时，会先读取变量对象顶层的 `key_type` / `value_type`。

解析器在后续编码阶段也兼容把字典描述放在 `value` 中，`export_gil_components.py` 导出的字典就是这种形式，但不建议让 LLM 主动生成该兼容格式。结构体内部的字典字段不属于顶层变量，必须使用下一节所示的对象值格式。

### 9.2 结构体内部的字典

如果 `.structs.json` 已为字典字段提供：

```json
{
  "name": "运镜点",
  "type": "dict",
  "dict_key_type_code": 3,
  "dict_value_type_code": 26,
  "dict_value_struct_id": 1077936130
}
```

则 JSON 可以只写：

```json
{
  "运镜点": {
    "entries": [
      {
        "key": 1,
        "value": [
          {
            "路径索引": 1,
            "路径点起点序号": 0,
            "路径点终点序号": 2,
            "镜头模版名": "镜头A",
            "运动时间（=等待切换时间）": 2.0
          }
        ]
      }
    ]
  }
}
```

如需显式写全，也可以：

```json
{
  "运镜点": {
    "key_type": "int",
    "value_type": "struct_list",
    "value_struct_id": 1077936130,
    "entries": []
  }
}
```

注意：自定义非空字典条目目前仍属于实验性编码。生成器会在 `.summary.json` 中产生 warning，必须在编辑器内验证。空字典相对稳妥。

## 10. UGC 对话框文件的格式示例

以下示例只对应：

`scripts/test/UGC对话框模板——元件版（超限模式资产）.structs.json`

它只用于展示一种较复杂的 JSON 形态。解析其他 GIA/GIL 时，必须换成本次解析器实际导出的结构体名称和字段。该示例对应的导出文件已经包含嵌套结构体 ID 和字典类型信息，因此无需额外写 `__struct_types__`：

```json
{
  "components": [
    {
      "name": "辉夜天空之城对话测试",
      "variables": [
        {
          "name": "对话",
          "type": "struct",
          "struct": "对话",
          "value": {
            "对话列表": [
              {
                "等待显示时间": 0.0,
                "标题": "辉夜",
                "副标题": "天空之城的入口",
                "内容": [
                  "你终于来了，旅行者。",
                  "抬头看那片云层，天空之城就沉睡在它的背后。"
                ],
                "内容显示时间（若需）": [],
                "节点图事件触发": {
                  "entries": []
                },
                "运镜点": {
                  "entries": []
                }
              },
              {
                "等待显示时间": 0.0,
                "标题": "玩家",
                "副标题": "",
                "内容": [
                  "天空之城？那不是传说里的地方吗？"
                ],
                "内容显示时间（若需）": [],
                "节点图事件触发": {
                  "entries": []
                },
                "运镜点": {
                  "entries": []
                }
              }
            ],
            "跳转对话": {
              "站桩对话": "",
              "选项卡": "",
              "边走边说": "",
              "黑幕": "",
              "CG": "",
              "中部文本": "",
              "CG插画": "",
              "触发节点图": "",
              "自定义对话类型": 0,
              "自定义对话类型的变量名": ""
            }
          }
        }
      ]
    }
  ]
}
```

## 11. 面向 LLM 的生成约束

要求 LLM 生成 components JSON 时，应使用以下约束：

1. 只输出合法 JSON，不输出 Markdown 代码围栏或解释。
2. 顶层固定为 `{"components": [...]}`。
3. 每个元件必须有 `name` 和 `variables`。
4. 每个变量必须有 `name` 和 `type`；非字典变量使用 `value`，字典变量使用 `key_type`、`value_type` 和 `entries`。
5. `type` 只使用本文档列出的标准类型名。
6. 结构体名称和字段名只能从目标 `.structs.json` 复制。
7. 优先使用结构体名称，不让 LLM 生成结构体 ID。
8. `.structs.json` 已包含嵌套类型 ID 时，不生成 `__struct_types__`。
9. 顶层字典变量使用平铺的 `key_type`、`value_type`、`entries`；结构体内字典字段使用对象值，已有字典元数据时可只生成 `{"entries": [...]}`。
10. 不生成 `id_policy`，除非调用方明确提供 ID 分配策略。
11. 不生成未定义字段；不使用注释、尾逗号、`NaN` 或 `Infinity`。

可直接使用的提示词骨架：

```text
根据用户需求和提供的 structs.json 生成 components JSON。

硬性要求：
- 只输出合法 JSON，不要输出 Markdown 或解释。
- 顶层必须是 {"components": [...]}。
- 结构体名称和字段名称必须逐字匹配 structs.json。
- 保持 JSON 原生类型，不要把数字和布尔值写成字符串。
- 不要猜测或生成 struct_id、element_struct_id、value_struct_id。
- structs.json 已提供嵌套结构体或字典类型信息时，直接依赖这些信息。
- 未明确要求的字段使用对应类型默认值。
```

## 12. 常见错误

| 现象或报错 | 原因 | 处理方式 |
| --- | --- | --- |
| `components JSON object must contain a components array` | 顶层缺少 `components` | 使用标准顶层对象 |
| `variable has no type/type_code` | 变量缺少类型 | 添加标准 `type` |
| `unsupported variable type` | 类型名称不受支持或拼写错误 | 使用“支持的类型”表中的标准名称 |
| `struct not found in extracted structs` | 结构体名称不在目标 `.structs.json` 中 | 重新导出并复制真实名称 |
| 结构体字段保持默认值 | 字段名不匹配或写在错误层级 | 对照 `fields[].name` 和 `value` 层级 |
| `fields are not defined by struct` | components 含有当前 schema 中不存在的字段 | 删除错误字段或改用本次导出的准确字段名 |
| `string contains an invalid Unicode surrogate` | 字符串含有无法编码为 UTF-8 的代理字符 | 让 LLM 重新生成该字符串或替换非法字符 |
| `only supports a default value safely` | 对尚未确认的 ID/object 类型写入了非默认值 | 使用默认值，或先补齐该 `type_code` 的编解码实现 |
| 嵌套结构体为空或报错 | 导出定义缺少嵌套结构体 ID | 添加 `__struct_types__` |
| 字典键值类型不正确 | 缺少或写错 `key_type` / `value_type` | 优先使用导出字段元数据，否则显式填写 |
| `.summary.json` 出现 dict warning | 使用了自定义非空字典 | 在编辑器中验证生成结果 |
| 输出文件已存在 | 未提供 `--overwrite` | 添加参数或更换输出路径 |
| 修改 JSON 后编辑器没有变化 | 没有重新构建 GIL | 再次执行构建命令并导入新文件 |

## 13. 推荐验证流程

每次首次适配一个新模板时：

1. 从目标 GIL/GIA 导出 `.structs.json`。
2. 先生成一个元件和一个简单变量。
3. 运行 `build_gil_components.py`。
4. 检查 `.summary.json` 中的 `warnings`。
5. 在编辑器中导入并核对变量类型和值。
6. 对复杂结构体和非空字典分别做最小样例。
7. 验证通过后再批量生成。

构建前的递归校验会阻止未知字段、类型不匹配、缺失嵌套结构体和非法 UTF-8；构建后的无损容器检查会验证头部长度、Protobuf wire 和原字节回环。它们仍不能证明所有变量值在游戏编辑器中的业务语义正确，因此未完全确认的 ID/object 类型和非空字典仍需编辑器实测。
