# T8 三合一程序（练习试下 + 答疑 + 档位选择）—— 验收报告

> 完成日期：2026-08-31 · 状态：✅ 完成并验收通过（Playwright 11 张截图，EXIT=0）

## 改动文件

| 文件 | 操作 | 说明 |
| --- | --- | --- |
| `frontend/index.html` | 修改 | 顶栏三 tab（复盘/练习/答疑）SPA 切换；练习视图（题库列表/题面棋盘/试下/判定/答案/下一题）；答疑视图；设置面板档位下拉 |
| `frontend/js/app.js` | 修改 | 练习数据流（library→detail→attempt）、WGo 官方点击落子试下、判定反馈、显示答案（CR 圆圈）；答疑 ask + 降级；档位（reviewProfile 生效于 analyze）；复盘"试摆"模式 |
| `frontend/css/style.css` | 修改 | 三视图与练习/答疑样式 |
| `scripts/playwright_m2.py` | 新增 | 验收脚本（11 步截图，自管后端） |
| `backend/config.yaml` / `settings.py` | 修改 | 档位提高：fast 400 / standard 1200 / fine 3000（原 200/600/1500） |

## 功能验证（Playwright 实测）

1. 练习 tab：Mock 3 题 → 真实题库 50 题（官子谱已导入部分）→ 选中题面 19 路白先；
2. 试下：棋盘点击落子（WGo 官方 click 交互）→ 判定 → "✗ 不对"反馈；
3. 显示答案：正解 F5 以圆圈标在棋盘上；
4. 答疑：提交问题 → 降级提示（未配 LLM 不烧钱）；
5. 档位：设置面板 fast → fine（PUT /system/settings 生效），复盘提交用所选档位（`reviewProfile` 闭环）；
6. 复盘回归：20 手 mock 复盘正常；复盘"试摆"模式摆子后可退出恢复棋谱定位。

## 修复的 bug

- **判定按钮一直 disabled**：`practiceTrial` 为模块级数组（存 WGo addObject 原始对象，不能被 Vue proxy），Vue computed 无法追踪其变化。修复：新增响应式计数 `practiceTrialN`，push/pop/reset 同步更新，按钮绑定改为 `attemptLoading || !practiceTrialN`。

## 遗留

- 答疑/讲解真实内容依赖 M2 的 LLM prompt 迭代（当前正确降级）；
- 练习页"做对自动下一题"与连做统计可在 M5 打磨期增强。
