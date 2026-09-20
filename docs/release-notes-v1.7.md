# 弈友 v2 · v1.7 发布说明（已发布：v1.7.6）

> **状态：已于 2026-09-20 发布** → https://github.com/nebulaeyes1456/GoCoachAI/releases/tag/v1.7.6
> 附件名 **YiYou-Setup.exe**（87 MB，SHA256 634ecd3b…c5dc4）。
> ⚠️ 经验：`gh release create` 上传**中文文件名**会被改成 `default.exe`——发布前先把安装包
> 复制成 ASCII 名（如 `YiYou-Setup.exe`）再上传。

---

## v1.7.7（厚题库版，待发布）

### 发布正文（复制这一段）

**弈友 v2（YiYou）**：Windows 上的 AI 围棋教练——KataGo 复盘 + AI 讲解 + 死活题库 + 人机对弈。本版带来**全量古典题库**与 **Linux 源码运行支持**。

### 本版新增

- **📚 全量古典题库（1,079 题）**：六本公版棋书（碁経衆妙、官子谱、发阳论、忘忧清乐集、玄玄棋经、玄览）共 2,624 个题面全部经 KataGo 局部聚焦验题，胜率 ≥60% 者入库——每道题都有引擎验证的正解与胜率。题库按书内顺序分三级难度（15K~8K / 7K~2K / 1D~3D）。加上题链，安装包内含 **44 条题链、1,124 道题**，首次启动自动导入。
- **🐧 Linux 支持（源码运行）**：`scripts/download_katago.py` 自动下载 Linux x64 引擎与模型，后端开箱适配；浏览器打开即可使用（无 GPU 自动回退 CPU 引擎）。
- 级位口径修正：古典题按书内顺序三分位（此前递归导入会拿整库总数分档，导致发阳论等高段死活书全被标成入门级）。
- 断点续跑优化：导入器在引擎分析前跳过已入库题目，续跑不再重复计算。

### 安装（Windows）

1. 下载 **`YiYou-Setup.exe`**（本页附件）；
2. 双击运行——装到 `%LOCALAPPDATA%\Programs\弈友`，自动建桌面/开始菜单快捷方式，**无需管理员权限**；
3. 首次启动自动导入全部题链与题库；AI 文字讲解需自填 DeepSeek key（可选）。

### Linux（源码运行）

```bash
git clone https://github.com/nebulaeyes1456/GoCoachAI.git
cd GoCoachAI
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/python scripts/download_katago.py
cp data/config.example.yaml data/config.yaml
./.venv/bin/python -m uvicorn backend.main:app --app-dir . --port 8765
# 浏览器打开 http://127.0.0.1:8765
```

### 系统要求（Windows 版）

- Windows 10 / 11（64 位）
- 约 1.2 GB 磁盘（含 KataGo 引擎与神经网络）
- 显卡可选：有 OpenCL 显卡更快，无显卡自动回退 CPU 版引擎

### 校验

```
文件：YiYou-Setup.exe
大小：（构建后填写）
SHA256：（构建后填写）
构建：2026-09-20（v1.7.7）；库内 44 条题链、1,124 道题（1,079 道古典死活题）
```

### 从源码运行（开发者）

同 v1.7.6；Windows 与 Linux 步骤见 README「Option B」。

---

## v1.7.6 发布正文（已发布，存档）

> 用法：GitHub → Releases → Draft a new release → Tag 填 `v1.7.5`（或 `v1.7`）
> → 标题填「弈友 v2 v1.7 · 死活题链」→ 正文粘贴下面「发布正文」部分
> → 附件上传 `弈友安装程序.exe` → Publish。

---

## 发布正文（复制这一段）

**弈友 v2（YiYou）**：Windows 上的 AI 围棋教练——KataGo 复盘 + AI 讲解 + 死活题库 + 人机对弈。本版重点是**死活题链（Growth Chains）**：题库从「平面题集」升级成**有来源、有脉络的成长树**。

### 本版新增

- **🔗 死活题链**：从定式（托退、双飞燕、小雪崩、大飞守角点三三、村正妖刀…）或**古典棋书**（碁経衆妙、官子谱、玄玄棋经、发阳论…）出发，长出后续的死活/对杀变化题。练习页「🔗 题链」入口 → 链列表 → 竖向时间线（第 1 变 → 第 n 变，含目标与难度）→ 一键「▶ 连续练习」，做完一题自动进入下一变。链上题带来源徽标「托退定式 · 第 3 变」。
- **⚡ 快速死活判定**：给任意棋形，秒级给出双方死活读数（谁活谁死/对杀气数/是否劫争/脱先损失），纯 KataGo 本地推演，不花钱、不联网。
- **📚 自带题库**：安装包内含 **44 条题链、41 道经引擎验证的题目**，首次启动自动导入，开箱即可练。
- 引擎与交互细节的一批改进（局部死活推演、同步讲棋、变化播放等，详见仓库 `更新日志.md`）。

### 安装

1. 下载 **`弈友安装程序.exe`**（本页附件）；
2. 双击运行——装到 `%LOCALAPPDATA%\Programs\弈友`，自动建桌面/开始菜单快捷方式，**无需管理员权限**；
3. 首次启动后：复盘/对弈/做题**全部可用**；若想要 AI 文字讲解与语音，在「设置」里填自己的 DeepSeek API key（约 ¥0.004/题），或改用本地 Ollama（`coach.provider: ollama`）。

### 系统要求

- Windows 10 / 11（64 位）
- 约 1.2 GB 磁盘（含 KataGo 引擎与神经网络）
- 显卡可选：有 OpenCL 显卡更快，无显卡自动回退 CPU 版引擎

### 校验

```
文件：弈友安装程序.exe
大小：91,332,916 字节（87 MB）
SHA256：634ecd3b71cacc0b8a3867ff88f4a4c1544618fcd9a677d3db08122c221c5dc4
构建：2026-09-20（v1.7.6）；已实装验证：装后启动正常，自带 44 条题链、题库有题
```

### 从源码运行（开发者）

```powershell
git clone https://github.com/nebulaeyes1456/GoCoachAI.git
cd GoCoachAI
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python.exe scripts\download_katago.py   # 引擎+模型不入库，必须单独下
copy data\config.example.yaml data\config.yaml         # 填自己的 key（可留空）
.\.venv\Scripts\python.exe backend\desktop.py
```

> 注意：仓库里**不含** API key、也不含任何用户数据；安装包由 `scripts/build_desktop.py` 打包时会剔除 config/数据库/棋谱并做密钥扫描。

---

## 打包者自查清单（发布前逐条过，**不上网页**）

- [ ] `scripts/batch`…（无此步，忽略）
- [ ] 重建 exe：`.\.venv\Scripts\python.exe scripts\build_desktop.py --clean`
      → 看到 `[build][SEC] key scan: clean` 才继续
- [ ] 产物自查：`dist\弈友\_internal\data\` 里**只有** `chains/` 与 `config.example.yaml`
- [ ] 复制到根目录：`copy dist\弈友\弈友.exe 弈友.exe`
- [ ] 重建安装程序：`.\.venv\Scripts\python.exe scripts\setup_installer.py`
- [ ] 算校验值：`certutil -hashfile 弈友安装程序.exe SHA256`
- [ ] 公开前再确认：仓库里 `git grep -I -E "sk-[A-Za-z0-9]{20,}"` **无命中**
