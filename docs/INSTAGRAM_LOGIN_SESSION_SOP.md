# Instagram 登录与暖 Session 最终方案

本方案解决以下重复问题：普通 Chrome 已登录但脚本接不到、全新 Chrome 被判
“登录信息有误”、只注入 `sessionid` 导致设备 Cookie 丢失、保存 session 后又
回退到密码冷登录。

## 固定原则

1. 登录优先级固定为：**暖 session → 完整浏览器 Cookie → 一次性人工登录**。
2. 不把“普通 Chrome 仍在线”理解为“密码一定还能在新设备登录”。旧会话和新
   设备密码验证是两条不同路径。
3. 已有普通 Chrome 登录态时，直接从本机 profile 导出 Cookie，不再新建 profile
   重新撞密码。
4. 必须保留导出的全部 Cookie；`sessionid`、`ds_user_id`、`csrftoken`、`mid`、
   `ig_did` 是最低要求。不能只复制 `sessionid`。
5. `data/session/instagrapi-*.json` 永不主动删除。Cookie 只在本机 `.secrets/`
   保存，权限必须为 `0600`。
6. 浏览器和采集客户端必须走同一代理出口。本机当前 Clash HTTP 入口为
   `http://127.0.0.1:7897`；实际运行以 `IG_PROXY` 为准。
7. 账号密码页面被拒绝、出现 429、challenge 或 checkpoint 后立即停止，不做
   连续重试。先回到可信普通 Chrome 检查会话，再重新导出完整 Cookie。

## 路径 A：普通 Chrome 已经登录（首选）

不关闭普通 Chrome，也不要求它预先开启 CDP：

```bash
cd /path/to/ins--collector-ultimate

/usr/bin/python3 scripts/export_chrome_profile_cookies.py \
  --profile Default \
  --domain instagram.com \
  --output .secrets/instagram-USERNAME.cookies.json

IG_PROXY=http://127.0.0.1:7897 \
.venv/bin/python scripts/instagram_session.py \
  --username USERNAME \
  --cookie-file .secrets/instagram-USERNAME.cookies.json \
  --update-account-pool
```

第二条命令会完成以下闭环：

- 校验 Cookie 文件权限和关键 Cookie；
- 校验 `sessionid` 与 `ds_user_id` 属于同一账号；
- 全量注入 instagrapi private/public Cookie jar；
- 复用已有移动端设备参数，避免无谓更换设备身份；
- 用临时候选 session 暖复载，并以 `account_info` 核对用户名；
- 只有验证成功才原子替换正式 session；
- 把完整 Cookie 合并进主账号池已有账号，禁止创建同名重复记录。

## 路径 B：本机没有任何已登录会话

每个账号使用独立 profile。浏览器由 shell 启动，Playwright/CDP 只负责连接，
脚本退出不会带走 Chrome：

```bash
cd /path/to/ins--collector-ultimate

INSTAGRAM_PROFILE=USERNAME CDP_PORT=9330 \
  scripts/start_instagram_cdp.zsh
```

在弹出的真实 Chrome 中人工完成一次登录和必要验证。成功看到主页后导出：

```bash
.venv/bin/python scripts/export_browser_cookies.py \
  --cdp-url http://127.0.0.1:9330 \
  --domain instagram.com \
  --output .secrets/instagram-USERNAME.cookies.json

IG_PROXY=http://127.0.0.1:7897 \
.venv/bin/python scripts/instagram_session.py \
  --username USERNAME \
  --cookie-file .secrets/instagram-USERNAME.cookies.json \
  --update-account-pool
```

不用浏览器时按同一个 profile 和端口停止：

```bash
INSTAGRAM_PROFILE=USERNAME CDP_PORT=9330 \
  scripts/stop_instagram_cdp.zsh
```

启停脚本会校验端口占用和 `--user-data-dir` 归属，不会把其他 Chrome 当成本次
实例，也不会根据未经验证的陈旧 PID 停止进程。

## 正常运行时的账号池行为

`scripts/account_pool.py` 的固定顺序：

1. 加载 `data/session/instagrapi-USERNAME.json`，同步 private/public Cookie jar，
   调用一次轻量接口验证；
2. 暖 session 失效时，从账号池读取**完整 Cookie**重新建立授权并保存；
3. 两者都失败时才允许既有的一次性密码冷登录纪律生效。

因此，成功执行 `instagram_session.py` 后，正式采集不会再从密码页开始。

## 故障判断

- 普通 Chrome 在线，新 Chrome 提示账号密码错误：优先判定为新设备风控或密码
  已变化，走路径 A，不重试密码。
- 导出后没有 `sessionid`：导出的 profile 没有登录，不能生成暖 session。
- `sessionid` 与 `ds_user_id` 不一致：文件混入不同账号 Cookie，停止使用。
- 暖复载用户名不一致：Cookie 归属标注错误；脚本会拒绝覆盖正式 session。
- 端口已被占用：更换 `CDP_PORT`，不得直接连接未知进程。
- `login_required/challenge/checkpoint`：停止该账号请求，保留现有文件，回到可信
  浏览器重新导出；不要删除 session 后反复冷登录。

## 安全边界

- 文档、命令输出和 Git 中不得出现密码、TOTP、Cookie 值或 session 内容。
- `.secrets/` 与 `data/session/` 已被 Git 忽略，但仍需保持 `0600`。
- Modash 等网站沿用其各自已登录 Chrome，会话不得混入 Instagram 账号池。
