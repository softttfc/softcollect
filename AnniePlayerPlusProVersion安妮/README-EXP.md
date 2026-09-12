# AnniePlayer V1 Preview — EXP 7.28 实验版改动记录

沙箱目录：`app/FB2Kversion/exp7.28`（完整独立工程 + 引擎源码，不污染稳定版）。
产物：`EXPexe/AnniePlayerSVexp-Setup.exe`。

## 1. 15 段均衡器（EQ）

| 文件 | 改动 |
|---|---|
| `engine/src/Eq.cs`（新增） | 15 段 RBJ peaking biquad 链（32Hz~16kHz，2/3 倍频程，Q≈1.41，±12dB clamp），系数不可变快照原子替换，音频线程无锁 |
| `engine/src/Outputs.cs` | `PcmFloatSource.Eq` 挂载点；Read() 中 EQ → 增益 → 计量 的处理顺序 |
| `engine/src/Engine.cs` | 新增 `eq.set` RPC（gains[15]/enabled），增益热更新不中断播放；换歌/换采样率自动重建滤波链 |
| `renderer/js/local/eq.js`（新增） | 全局单例 Store（localStorage 持久化）+ 订阅同步；`mountPanel()` 双界面共用 DOM；粒子舞台悬浮面板（#controls 注入 EQ 按钮）；启动带重试应用 |
| `renderer/js/local/fb2k.js` | 底部 EQ 抽屉（`.f2-eq-dock`），工具栏 EQ 按钮开关 |
| `css/app.css` / `css/fb2k.css` | EQ 面板双主题样式 |

同步机制：两个面板订阅同一 Store，任一界面调整即 emit → 另一面版实时刷新；引擎端全局同一 EqChain 实例。

## 2. 文件索引重构（多线程 Worker）

| 文件 | 改动 |
|---|---|
| `main/scanWorker.js`（新增） | worker_threads 递归扫描（深度≤12，批量 100 首回传），每 200 项让出事件循环保证 cancel 即时生效；元数据批量解析（music-metadata，并发 4，50 条/批） |
| `main/main.js` | ScanManager：Worker 生命周期/崩溃重建/超时兜底；`lib:scanStart`/`lib:scanCancel` IPC；`lib:metaBatch` 走 Worker；`lib:pickFolder` 不再同步扫描；`lib:removeFolder` 内存过滤即时生效 |
| `main/preload.js` | scanStart / scanCancel / onScanEvent 桥接 |
| `renderer/js/local/player.js` | 扫描进度浮层（"扫描中…已发现 X 首" + 取消按钮，250ms 节流增量渲染）；批次追加 tracks |
| `renderer/js/local/fb2k.js` | 左栏改为递归文件夹树（复用 player.js `buildFolderTree`，与粒子舞台同源同行为），展开/折叠 + 递归计数 |

降级策略：Worker 创建失败 → 主进程同步扫描兜底；Worker 崩溃 → 事件通知 + 重新拉取曲库 + meta 降级主进程解析。

## 3. 性能优化

- `renderer/js/local/listWorker.js`（新增）：>1000 首时排序/过滤 offload Web Worker（Intl.Collator 拼音排序离开主线程），player.js / fb2k.js 双界面接入，渲染代际防过期写入，Worker 失败同步兜底
- `player.js renderTracks`：虚拟滚动（可视窗口 ±400px，固定行高 48/30），5000+ 首滚动保持 60fps；曲目行构建抽取为 `buildTrackRow`
- 沿用既有优化：fb2k 虚拟滚动 + 排序缓存、metaCache 持久化、按需懒加载封面

## 4. 追加修复（7.28 第二轮）

- **粒子舞台点歌无响应**：`ensureTagsForSort` 对解析失败的文件（DSD/损坏/无标签容器）永远视为 missing，
  与 `renderTracks` 形成无限重渲染循环，行节点被反复替换导致 click 无法派发。
  修复：会话级负缓存标记（`state.tagCache[p] = {}`），循环有界终止（经 CDP 探针实测验证）。

## 已知权衡

- EQ 变更走系数热更新，理论上无爆音（状态保留、快照替换），未做增益斜坡平滑
- 虚拟滚动下粒子舞台的分组头不再 sticky（与绝对定位冲突）
- 扫描 Worker 顺序执行任务：扫描中发起的 meta 请求会排队到扫描让出时隙处理
