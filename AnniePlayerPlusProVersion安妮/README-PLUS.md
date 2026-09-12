# AnniePlayerPlusVersion —— 改动记录

基线：EXP 7.28 全部成果（15 段 EQ / Worker 曲库扫描 / 大列表 Worker 排序 / 虚拟滚动 / 点击委托修复）。

## V1.1.2 追加

- **FB2K 暗色模式**（护眼主题，`#fb2k-root.f2-dark` 全组件覆盖）：
  - 配色：主背景 #17191f / 面板 #1e2129~#1b1e26 / 输入 #262b35；主文本 #e4e7ee（≈13:1）、
    次级 #9aa1b0（≈7:1）、边框 #3a3f4d（≥3:1），播放行 #2f4a57、选中 #31435a、强调 #7fc4d8/#5aa0c8
  - 覆盖：标题栏/菜单栏+下拉/左栏树/中央列表(行/分组/评级/封面)/右栏(封面/元数据/歌词/频谱)/
    底栏(工具钮/进度/传输/音量)/右键菜单/属性弹窗/EQ 抽屉/滚动条（频谱柱 JS 色随主题提亮）
  - 三入口同步切换：工具栏 🌙 按钮 / 快捷键 Ctrl+Shift+D / 设置面板「FB2K 界面 · 外观」；
    `annie-f2-dark-changed` 事件同步设置面板状态
  - 持久化 localStorage `annieplayer.fb2k.dark`，build 首帧前应用无闪烁；右键菜单与弹窗改挂
    `#fb2k-root` 内以继承暗色类；legacy 主题零影响
- 产物：`AnniePlayerPlusVersionSetup/AnniePlayerPlusVersionSetup-V1.1.2.exe`

## V1.1.1 追加

- **跨主题索引定位**：播放中切换界面主题时，目标主题左侧索引自动定位到正在播放的文件——
  - 粒子舞台：自动进入播放文件所在文件夹的曲目视图，虚拟列表滚动到播放行（`locatePlayingLegacy`）
  - FB2K：目录树自动展开祖先链并选中播放文件所在文件夹，列表滚动到播放行（`locatePlayingFb2k`）
  - 经 `annie-theme-changed` 事件驱动；流媒体/库外文件保持当前视图不打扰
- 产物：`AnniePlayerPlusVersionSetup/AnniePlayerPlusVersionSetup-V1.1.1.exe`

## V1.1.0 追加

- **字体抗模糊**：全局 `-webkit-font-smoothing: antialiased` + `text-rendering: optimizeLegibility`；
  次级小字号文本（t-sub/时间码/chip）提一档；悬浮曲目信息阴影收窄去虚边
- **侧栏平铺文件夹导航**（粒子舞台）：
  - 默认显示平铺文件夹视图——所有含曲目的文件夹平铺（无子文件夹的与其他子文件夹同级），
    浅色字体标注归属主文件夹；此视图不显示曲目
  - 点击进入文件夹 → 仅显示其内曲目（递归含子文件夹，复用 isPathUnder 过滤），顶部出现返回栏
  - 旧逻辑保留：🗂 文件夹图标网格视图不变；FB2K 树形索引不变；排序/搜索/虚拟滚动/点歌链路不变
- 产物：`AnniePlayerPlusVersionSetup/AnniePlayerPlusVersionSetup-V1.1.0.exe`

## VP1.0.0 基线

产物：`AnniePlayerPlusVersionSetup/AnniePlayerPlusVersion-Setup.exe`。

## 1. 布局（方案A：传输条三段分层）
- `index.html`：`#controls` 重组为 `.ctl-group.transport`（⏮ ▶ ⏭ ⏹，主键 56px）/
  `.ctl-group.tools`（♡收藏当前曲目 / 📊频谱面板 / EQ，居中）/ `.ctl-group.sys`（音量 / 🎧设备）
- 设备选择器移出底栏 → `#device-pop` 弹层（🎧 按钮开合、点击外部关闭、Esc 级关闭按钮）
- 进度条加高 6px，悬停加粗至 10px；侧栏宽度 `clamp(260px, 24vw, 380px)`

## 2. 四配色系统（全部通过 WCAG AA 实算）
- `app.css`：`:root[data-palette]` 四套变量（gold 暗夜金 / aurora 靛蓝极光 / jade 翡翠深空 / day 白昼），
  边框/轨道/禁用态全面提级至 3:1+；硬编码色收编为 CSS 变量；白昼适配覆盖（弹层/设置/浮窗）
- `settings.js`：`ui.palette` 持久化（store.ui + localStorage 早标记防闪屏），
  设置面板新增「外观 · 配色方案」卡片区，即时切换
- `index.html`：head 早标记 `data-palette`

## 3. 年轻化 / 微交互
- 字级令牌（12/13/14/16/20/24）+ 间距令牌（4/8/16/24）+ 弹簧曲线 `--ease-spring`
- 播放键回弹（playPop）、切歌封面交叉淡入 + 文本逐行滑入（np-enter）、
  收藏爆裂（favPop）、按钮下沉反馈、扫描浮层呼吸、弹层 popIn、`:focus-visible` 焦点环、
  `prefers-reduced-motion` 全局降级

## 4. 交互健壮性
- `player.js`：曲目行点击改为容器级 pointerdown/pointerup 委托——标签后台加载替换行节点时
  （按下与抬起之间）点击仍可达（EXP 版 per-row onclick 在该竞态下丢点击）
- `viz.js`：`annieViz.toggleBar()` 暴露给底栏频谱按钮
- `eq.js`：EQ 按钮注入 `.ctl-group.tools`

## 5. 品牌与打包
- `build/make-icon-plus.ps1`：金环图标（暗金描边 + 亮金主环，7 尺寸 PNG-in-ICO）
- 版本 1.0.0（VP1.0.0），快捷方式「安妮播放器Plus」，安装器文字「本应用由无敌章鱼哥开发」不变
- appId `com.mineradio.local.plus`，与稳定版/EXP 版隔离安装

## 回归测试（CDP 实测通过）
布局三段分层 / 设备弹层开合 / 4 配色切换+持久化 / 真实鼠标点歌 / 收藏切换 /
EQ 15 段+预设 / FB2K 主题渲染 / 零异常。
