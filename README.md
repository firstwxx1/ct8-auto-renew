# ct8-auto-renew

CT8（panel.ct8.pl）免费主机自动续期。

## 原理

CT8 免费账号长期不登录会被停用，而**登录面板本身就是续期动作**，没有别的机关。
这个脚本定期登录一次，把不活跃计时器清零。

panel.ct8.pl 是标准 Django 应用（nginx + Phusion Passenger），登录表单：

```
POST /login/
  csrfmiddlewaretoken  <- 从 GET /login/?next=/ 的表单里取
  username
  password
  next=/
```

**没有验证码，没有 Cloudflare 挑战**，所以纯 HTTP POST 就够。
不需要 puppeteer / Chrome / xvfb —— 这是本脚本和主流方案最大的区别：
`lopins/serv00-auto-scripts` 用 `headless: false` 跑 Chrome，CI 里必须套 `xvfb-run`，
又重又慢。这里全标准库，一次跑完不到两秒。

登录成功判定（**正向判定，抗页面改版**）：

1. 落地页含 `/logout/` 且不含 `data-login-form` —— 主判定；
2. 或者拿到了 `sessionid` cookie 且不含 `data-login-form` —— 兜底判定。

命中任意一条即视为成功。这样即使 CT8 哪天改了面板样式、把 `/logout/`
换成图标，也不会把一次**成功**的登录误报成 FAIL。

## 配置

在仓库 `Settings -> Secrets and variables -> Actions` 建 Secret：

| Secret | 必填 | 说明 |
|---|---|---|
| `ACCOUNTS_JSON` | 是 | 账号列表，见下 |
| `TELEGRAM_BOT_TOKEN` | 否 | 失败/成功推送 |
| `TELEGRAM_CHAT_ID` | 否 | 同上 |

`ACCOUNTS_JSON` 格式：

```json
[
  { "username": "你的CT8用户名", "password": "你的CT8面板密码" }
]
```

多个账号就往数组里加。

> 注意：填的是**面板登录密码**，不是 SSH 密码，两个不一样。

## 定时

`17 3 * * 1` —— 每周一 UTC 03:17 跑一次，**真正 7 天间隔**。
（旧值 `17 3 */7 * *` 在「每月第几日」上步进 7，实际命中 1/8/15/22/29 号，
月末会缩成 3 天。做保活无害，但名不副实。）
CT8 的停用窗口远长于 7 天，足够安全。想手动触发去 Actions 页点 Run workflow。

## 关键细节

### keepalive（保活提交）

**GitHub 会在仓库 60 天无活动后自动停用定时任务**，很多 fork 莫名其妙失效就是栽在这。
本仓库每次跑完都会提交一个 `last_run.txt` 时间戳，仓库有活动，cron 就不会被关掉。
别把那一步删了。

### 为什么保活那步是 `continue-on-error: true`

保活提交如果失败（网络抖动、rebase 冲突），**不应该把一次成功的续期染红**。
记账失败和续期失败是两个信号，只允许后者让 job 变红。
配合 3 次重试，偶发网络问题也能自愈。

### concurrency

`concurrency.group: ct8-renew` 保证手动触发的 run 不会和定时的 run 在
push 上打架。

## 本地测试

不填真密码也能验证链路通不通 —— 故意用错密码，能正确报失败就说明
csrf 提取、POST、成功判定三件事都是通的：

```bash
python renew.py --selftest
```

预期输出：

```
PASS - wrong credentials correctly rejected; csrf + POST + detection work
```

## 依赖

零。Python 3.8+ 标准库即可，无需 `pip install`。

## 免责说明

账号密码只存在 GitHub Secrets 里，不进代码库。仓库保持私有。
