# 定时检查 → 需求（schedule intent）

一个定时任务跑检查，查出问题就登记成一个需求：`brain intent add --source schedule`。
下面用"每周查一次过期依赖"做例子：KiroCrew 的 cron 和系统 crontab 各一种写法，选一种就行。

先记住四点：

- **需求记在谁名下**：跑任务的那个人（`GITLAB_TOKEN`、`~/.brain/gitlab-token` 或 `git config brain.user`）。
  服务器钩子要求 claim.json 里的人就是推送的人，机器人账号（`bot_users`）不能登记。
- **不会重复**：同样的来源、标题和正文只登记一次。所以正文里别放时间戳；想每周一个，就在标题里写上周次。
- **检查的输出是不可信的数据**：里面有密钥、邮箱、人名、金额、像指令的行，`brain intent add` 直接拒绝（不打码），
  任务以非 0 退出。别把原始日志整段塞进正文，先挑出要的几行。
- 只读检查。登记以后由人决定谁做：`brain intent list` 看，`brain start <slug> --key <KEY>` 开工。

## 检查脚本

放在项目外面，比如 `~/brain-jobs/deps-check.sh`（参数是项目的 clone）：

```sh
#!/bin/sh
# 每周：过期依赖 -> 一个 schedule 需求。列表不变就不会再登记一次。
set -eu
cd "$1"
git pull -q --ff-only
list=$(npm outdated 2>/dev/null | awk 'NR > 1 {print $1, $2, "->", $4}' | sort || true)   # 不用 name@version：像邮箱
[ -z "$list" ] && exit 0
body=$(mktemp)
trap 'rm -f "$body"' EXIT
{ echo "npm outdated 列出这些依赖有新版本："; echo "$list" | sed 's/^/- /'; } > "$body"
brain intent add --source schedule --title "过期依赖 $(echo "$list" | wc -l | tr -d ' ') 个" \
  --body-file "$body" --acs "升级后 lean verify 通过" "破坏性变更写进 decisions.md" --refs package.json
```

## 写法一：KiroCrew 的 cron（零 token，不叫 agent）

```bash
kirocrew cron add 'pay-deps-weekly' 'weekly dependency check for pay' \
  --cron '0 9 * * MON' --timezone Asia/Hong_Kong \
  --command 'PATH="$HOME/brain-kit/bin:$PATH" sh "$HOME/brain-jobs/deps-check.sh" "$HOME/work/pay"' \
  --timeout 600 --timeout-secs 700 --no-persistent-session --minimal-context --auto-pause-after 3
```

- `--command` 跑一条 shell 命令，不调模型；`message` 只是记录。连续失败 3 次（比如被内容检查拒了）自动暂停。
- 先手动跑一次看结果：`kirocrew cron list` 找到 job id，再 `kirocrew cron trigger <job-id>`。
- 要停：`kirocrew cron pause <job-id>`；要删：`kirocrew cron remove <job-id>`。
- 还没在真的 KiroCrew 上验证：`--command` 有沙箱，`git pull` 和推 claim 分支需要它放行网络和 git。

## 写法二：系统 crontab

`crontab -e`（用需求负责人的系统账号）：

```
# 分 时 日 月 周  命令
0 9 * * 1  PATH=/opt/brain-kit/bin:/usr/bin:/bin sh $HOME/brain-jobs/deps-check.sh $HOME/work/pay >> $HOME/.brain/cron.log 2>&1
```

身份用 `~/.brain/gitlab-token`（权限 600，`brain` 自己会读），不要把 token 写进 crontab。

## 别的定时检查也一样

把 `deps-check.sh` 里的检查换掉就行，比如 `pip list --outdated`、覆盖率低于门槛的模块、过期的特性开关。
告警跟进和技术债一般由人直接登记：

```bash
brain intent add --source alert --title "支付回调超时告警一周 12 次" --body-file alert-summary.md
brain intent add --source debt  --title "退款模块的重试逻辑重复了三份" --body "..." --refs src/pay/refund/
```
