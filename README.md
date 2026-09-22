# 课程表插件 for AstrBot

一个为 AstrBot 设计的课程表插件：绑定 `.ics` 课表文件后，可以查询自己和群友的课程安排、本周上课排行，并支持 AI 主动调用；在 QQ 官方机器人（官bot）上还可以用 Markdown 卡片 + 按钮的形式展示。

## ✨ 功能特性

*   **绑定 .ics 课表**：发送 `/绑定课表` 后上传 `.ics` 日历文件即可，支持群聊和私聊，可自定义显示昵称。
*   **绑定码复用**：绑定成功后会得到一个绑定码，在其他群或私聊发送 `/关联课表 绑定码` 即可直接复用，不用重复上传（官bot 群聊无法直接发文件时尤其有用）。
*   **今日 / 明日课表**：查看自己今天还有什么课、明天有什么课，以精美图片展示。
*   **群友课程总览**：一条指令看全群已绑定用户正在上 / 下一节 / 明天第一节的课，方便约饭、约游戏。
*   **本周上课排行**：统计本周到现在为止群友已经上过的课时与节数。
*   **AI 主动调用**：注册了 `course_schedule_query` 函数工具，直接对 AI 说“我今天还有什么课”“这周课多不多”即可。
*   **官bot Markdown 模式**：在 QQ 官方机器人上可选用 Markdown 卡片渲染课表，附带「今日课表」「课表帮助」等按钮，每个指令可单独开关。
*   **自定义字体**：把 `.ttf` / `.otf` 字体文件放入插件目录即可自动应用到图片。

## 📝 命令列表

| 命令 | 功能描述 |
| :--- | :--- |
| `/课表帮助` | 功能菜单。官bot Markdown 模式下为带按钮的卡片菜单。 |
| `/绑定课表 [昵称]` | 发送后 60 秒内在当前会话上传 `.ics` 文件完成绑定。昵称可选，官bot 拿不到昵称时建议填写。 |
| `/关联课表 [绑定码] [昵称]` | 用绑定码把已绑定的课表复用到当前会话；不带参数时显示自己的绑定码。 |
| `/解绑课表` | 解除当前会话的课表绑定。 |
| `/查看课表` | 查看自己今天还有什么课。 |
| `/查看明日课表` | 查看自己明天有什么课。 |
| `/群友在上什么课` | 群友当前正在上 / 下一节要上的课。仅群聊。 |
| `/群友明天上什么课` | 群友明天第一节课。仅群聊。 |
| `/本周上课排行` | 本周上课时长排行榜。仅群聊。 |

## 🤖 AI 主动调用

插件注册了一个函数工具 `course_schedule_query(scope)`，`scope` 取 `today` / `tomorrow` / `week`。工具会把对应范围内的全部课程（含起止时间、地点）和当前日期时间一起返回给模型，由模型自行判断哪些课已结束、正在进行或尚未开始，因此可以直接问：

*   “我今天还有几节课？”
*   “明天第一节几点？”
*   “这周三有什么课？”

未绑定课表时，工具会返回绑定引导。

## ⚙️ 官bot Markdown 模式

在插件配置中开启 `markdown_mode` 后，QQ 官方机器人（`qq_official` / `qq_official_webhook`）上的指令会改为发送 Markdown 卡片并附带按钮；其他平台不受影响。

| 配置项 | 说明 | 默认 |
| :--- | :--- | :--- |
| `markdown_mode` | Markdown 模式总开关 | 关 |
| `markdown_buttons` | 卡片是否附带按钮（机器人没有自定义按钮权限时关闭） | 开 |
| `markdown_commands.help` | 课表帮助使用 Markdown 菜单 | 开 |
| `markdown_commands.personal` | 查看课表 / 查看明日课表使用 Markdown | 开 |
| `markdown_commands.group` | 群友在上什么课 / 群友明天上什么课使用 Markdown | 关（默认仍发图片） |
| `markdown_commands.ranking` | 本周上课排行使用 Markdown | 开 |

Markdown 卡片发送失败（例如机器人没有相应权限）时会自动回退为图片 / 文本。

> 官bot 群聊里无法直接给机器人发文件，推荐先在私聊绑定，再到群里发送 `/关联课表 绑定码`。

## ❓ 如何获取 .ics 文件？

将课表导入课表软件（如 **WakeUp 课程表**），在软件设置中选择“导出”，导出为日历文件（`.ics`）即可。

## 🖼️ 效果预览

**群友课表样式 (`/群友在上什么课`)**

*<img width="800" height="440" alt="ccee4521fc5822da47458ff04c3086d0" src="https://github.com/user-attachments/assets/a4d71e19-43ed-4449-a889-4eeca370c118" />*

**个人课表样式 (`/查看课表`)**

*<img width="800" height="380" alt="d17a5c9144750086f306a21e795c611b" src="https://github.com/user-attachments/assets/8209ce65-09fd-4373-9100-1fbfbf8ceeb9" />*

**排行榜样式 (`/本周上课排行`)**

*<img width="900" height="460" alt="6b2ef146e07647930297672739e15249" src="https://github.com/user-attachments/assets/ce893ac1-cfca-479d-9924-00e2e017249b" />*

## 📁 文件结构

*   `main.py` — 插件入口：指令、绑定流程、LLM 工具与官bot 卡片分发。
*   `schedule_helper.py` — 绑定记录、课程读取与筛选、排行统计、LLM 文本格式化。
*   `ics_parser.py` — 解析 `.ics` 文件并展开重复课程，按周缓存。
*   `image_generator.py` — 渲染个人课表、群友总览、排行榜图片。
*   `qq_markdown.py` — 官bot Markdown 卡片渲染、按钮构造与发送。
*   `data_manager.py` — 数据目录与用户绑定数据的持久化。
*   `constants.py` — 图片样式常量。
*   `_conf_schema.json` — 插件配置项定义。
*   `metadata.yaml` / `requirements.txt` / `CHANGELOG.md`。

## 🤝 贡献

欢迎提交 Pull Request 或 Issue 来改进这个插件！
