using System.Text.Json.Nodes;
using NAudio.Wave;

namespace MineEngine;

/// <summary>引擎核心：命令分发 + 播放状态机。</summary>
public sealed class Engine
{
    private readonly Rpc _rpc = new();
    private readonly object _gate = new();
    private static readonly DateTime StartedAt = DateTime.UtcNow; // Pro beat0.0.1：调试面板运行时长

    private IOutputBackend? _backend;
    private string _backendKind = "wasapi";
    private string? _backendDeviceId;   // wasapi: MMDevice ID；asio: 驱动名

    private TrackInfo? _track;
    private FfmpegPcmStream? _pcm;
    private PcmFloatSource? _source;
    private double _offsetSec;
    private int _decodeRate;
    private bool _resampled;
    private bool _playing;
    private bool _ended;
    private float _gain = 1.0f;
    private double[] _eqGains = new double[EqChain.BandCount]; // EXP 7.28：15 段 EQ 增益（dB）
    private bool _eqEnabled = true;
    /* ---------------- Pro beat0.0.1：音质链路增强 ---------------- */
    private string _dsdMode = "pcm";       // pcm（转 PCM）| dop（DoP 直通）| native（ASIO DSD，暂回退）
    private int _bufferMs = 50;            // 独占缓冲 50–500ms
    private bool _preload;                 // 整轨预载到内存
    private double _crossfadeSec;          // 交叉淡入时长（0=关闭）
    private bool _autoPreamp = true;       // 自动前级补偿（防削波）
    private bool _limiter = true;          // 输出软限幅器
    private float _loudGain = 1.0f;        // 响度归一化增益（线性，由渲染侧按 R128 计算下发）
    private CrossfadeMixer? _mixer;
    private DsfReader? _dsf;
    private DopSource? _dopSource;
    private bool _dopActive;
    private string? _headers;          // 当前网络流的 HTTP 头（seek 重放时复用）
    private int _playGeneration;       // 播放代际：递增以识别新播放请求，防止竞态
    private readonly object _playGate = new(); // 播放互斥锁：串行化设备关键区（StopAll→打开→预缓冲→启动）

    private readonly Timer _positionTimer;

    public Engine(Rpc rpc)
    {
        _rpc = rpc;
        rpc.OnRequest += HandleAsync;
        _positionTimer = new Timer(_ => TickPosition(), null, Timeout.Infinite, Timeout.Infinite);
    }

    private Task<object?> HandleAsync(string method, JsonObject p)
    {
        object? result = method switch
        {
            "engine.info" => new
            {
                version = "1.0.1",
                backend = _backendKind,
                ffmpeg = Toolchain.FfmpegPath,
                ffprobe = Toolchain.FfprobePath,
                ffmpegFound = Toolchain.FfmpegFound,
                ffprobeFound = Toolchain.FfprobeFound,
                hint = (Toolchain.FfmpegFound && Toolchain.FfprobeFound) ? "" : Toolchain.MissingHint
            },

            "devices.list" => (object)new
            {
                wasapi = WasapiExclusiveBackend.Enumerate().Select(d => new
                {
                    id = d.Id,
                    name = d.Name,
                    // Pro：DoP 能力探测（176.4kHz/24bit 独占），不支持时前端灰显 DoP 选项；
                    // 个别虚拟设备属性缺失会抛 KeyNotFound，逐台隔离
                    dop = ProbeDopSafe(d.Id)
                }),
                asio = AsioBackend.Enumerate(),
                current = new { kind = _backendKind, id = _backendDeviceId }
            },

            "devices.select" => SelectBackend(p["kind"]?.GetValue<string>() ?? "wasapi", p["id"]?.GetValue<string>()),

            "probe" => ProbeGuarded(Req(p, "path")),

            "play" => Play(Req(p, "path"), p["offsetSec"]?.GetValue<double>() ?? 0, p["headers"]?.GetValue<string>(), p["loudGain"]?.GetValue<double>() ?? 1.0),

            "play.crossfade" => PlayCrossfade(Req(p, "path"), p["headers"]?.GetValue<string>(), p["loudGain"]?.GetValue<double>() ?? 1.0),

            "pause" => Pause(),
            "resume" => Resume(),
            "stop" => StopAll(emitState: true),

            "seek" => Seek(p["seconds"]?.GetValue<double>() ?? 0),

            "volume.set" => SetVolume(p["gain"]?.GetValue<double>() ?? 1.0),

            "eq.set" => SetEq(p),

            /* ---------------- Pro beat0.0.1 ---------------- */
            "dsd.setMode" => SetDsdMode(p["mode"]?.GetValue<string>() ?? "pcm"),
            "buffer.set" => SetBuffer(p["ms"]?.GetValue<int>() ?? 50, p["preload"]?.GetValue<bool>() ?? false),
            "dsp.set" => SetDsp(p),
            "loud.set" => SetLoudGain(p["gain"]?.GetValue<double>() ?? 1.0),
            "crossfade.set" => SetCrossfade(p["seconds"]?.GetValue<double>() ?? 0),

            "asio.panel" => ShowAsioPanel(),

            // Pro beat0.0.1：调试面板——解码缓冲水位 / 引擎运行时长 / 播放代际
            "stats" => (object)new
            {
                ok = true,
                bufferedBytes = _pcm?.QueuedBytes ?? 0,
                uptimeSec = (DateTime.UtcNow - StartedAt).TotalSeconds,
                playGeneration = _playGeneration,
                crossfadeSec = _crossfadeSec,
                dsdMode = _dsdMode
            },

            "shutdown" => Shutdown(),
            _ => throw new InvalidOperationException("未知方法: " + method),
        };
        return Task.FromResult(result);
    }

    private static string Req(JsonObject p, string key)
        => p[key]?.GetValue<string>() ?? throw new ArgumentException("缺少参数: " + key);

    private object SelectBackend(string kind, string? id)
    {
        StopAll(emitState: false);
        lock (_gate)
        {
            _backend?.Dispose();
            _backend = null;
            _backendKind = kind;
            _backendDeviceId = id;

            if (kind == "asio")
            {
                var name = id ?? AsioBackend.Enumerate().FirstOrDefault()
                    ?? throw new InvalidOperationException("未找到任何 ASIO 驱动。");
                _backend = new AsioBackend(name);
                _backendDeviceId = name;
            }
            else
            {
                var dev = (id is null ? WasapiExclusiveBackend.GetDefault() : WasapiExclusiveBackend.FindById(id))
                    ?? throw new InvalidOperationException("WASAPI 设备不存在: " + id);
                _backend = new WasapiExclusiveBackend(dev);
                _backendDeviceId = dev.ID;
            }
        }
        _rpc.Emit("backend", new { kind = _backendKind, device = _backend!.DeviceName });
        return new { kind = _backendKind, id = _backendDeviceId };
    }

    private IOutputBackend EnsureBackend()
    {
        if (_backend is null) SelectBackend(_backendKind, _backendDeviceId);
        return _backend!;
    }

    private object SetVolume(double gain)
    {
        _gain = (float)Math.Clamp(gain, 0, 1.5);
        lock (_gate) { if (_source is not null) _source.Gain = _gain; }
        return new { gain = _gain };
    }

    /// <summary>
    /// EXP 7.28：15 段均衡器。params: { gains: double[15], enabled: bool }
    /// EQ 作用于解码后的 float PCM（PcmFloatSource），增益实时热更新、无爆音、不中断播放；
    /// 换歌/换采样率时新源会按当前增益重建滤波链，双界面共享同一实例。
    /// </summary>
    private object SetEq(JsonObject p)
    {
        if (p["gains"] is JsonArray arr)
            for (int i = 0; i < EqChain.BandCount && i < arr.Count; i++)
                _eqGains[i] = arr[i]?.GetValue<double>() ?? 0;
        if (p["enabled"] is JsonNode en) _eqEnabled = en.GetValue<bool>();
        lock (_gate) { ApplyEqLocked(); }
        return new { ok = true, enabled = _eqEnabled, gains = _eqGains };
    }

    /// <summary>把当前 EQ 增益挂载到活动源（须持有 _gate）。</summary>
    private void ApplyEqLocked()
    {
        if (_source is null) return;
        if (!_eqEnabled) { _source.Eq = null; return; }
        var eq = _source.Eq;
        if (eq is null || eq.SampleRate != _source.WaveFormat.SampleRate)
            _source.Eq = eq = new EqChain(_source.WaveFormat.SampleRate, _source.WaveFormat.Channels);
        eq.Update(_eqGains);
    }

    /// <summary>Pro：把防削波链路（自动前级 + 限幅器）应用到活动源（须持有 _gate）。</summary>
    private void ApplyDspLocked()
    {
        if (_source is null) return;
        double maxPos = 0;
        if (_eqEnabled) foreach (var g in _eqGains) if (g > maxPos) maxPos = g;
        _source.Preamp = _autoPreamp ? (float)Math.Pow(10.0, -maxPos / 20.0) : 1.0f;
        _source.Limiter = _limiter;
        _source.LoudGain = _loudGain;
    }

    /* ---------------- Pro beat0.0.1：新 RPC ---------------- */

    /// <summary>Pro：DoP 能力安全探测（设备属性缺失/消失时返回 false 而不是让整个 devices.list 失败）。</summary>
    private static bool ProbeDopSafe(string id)
    {
        try
        {
            return WasapiExclusiveBackend.FindById(id) is { } dev
                && new WasapiExclusiveBackend(dev).SupportsDop(2822400);
        }
        catch { return false; }
    }

    private object SetDsdMode(string mode)
    {
        _dsdMode = mode is "dop" or "native" ? mode : "pcm";
        return new { ok = true, mode = _dsdMode };
    }

    private object SetBuffer(int ms, bool preload)
    {
        _bufferMs = Math.Clamp(ms, 50, 500);
        _preload = preload;
        lock (_gate) { if (_backend is WasapiExclusiveBackend w) w.BufferMs = _bufferMs; }
        return new { ok = true, ms = _bufferMs, preload = _preload };
    }

    private object SetDsp(JsonObject p)
    {
        if (p["autoPreamp"] is JsonNode ap) _autoPreamp = ap.GetValue<bool>();
        if (p["limiter"] is JsonNode lm) _limiter = lm.GetValue<bool>();
        lock (_gate) { ApplyDspLocked(); }
        return new { ok = true, autoPreamp = _autoPreamp, limiter = _limiter };
    }

    private object SetLoudGain(double gain)
    {
        _loudGain = (float)Math.Clamp(gain, 0.05, 4.0); // -26dB ~ +12dB
        lock (_gate) { if (_source is not null) _source.LoudGain = _loudGain; }
        return new { gain = _loudGain };
    }

    private object SetCrossfade(double seconds)
    {
        _crossfadeSec = Math.Clamp(seconds, 0, 10);
        return new { ok = true, seconds = _crossfadeSec };
    }

    private object ShowAsioPanel()
    {
        if (_backend is AsioBackend ab) ab.ShowControlPanel();
        return new { ok = true };
    }

    private object Shutdown()
    {
        Task.Run(async () => { await Task.Delay(100); Environment.Exit(0); });
        return new { ok = true };
    }

    // ---------------- 播放控制 ----------------

    private static TrackInfo ProbeGuarded(string path)
    {
        if (!Toolchain.FfprobeFound) throw new InvalidOperationException(Toolchain.MissingHint);
        return FfmpegPcmStream.Probe(path);
    }

    private object Play(string path, double offsetSec, string? headers = null, double loudGain = 1.0)
    {
        if (!Toolchain.FfmpegFound || !Toolchain.FfprobeFound)
            throw new InvalidOperationException(Toolchain.MissingHint);
        // 网络流地址跳过本地文件存在性检查
        if (!FfmpegPcmStream.IsUrl(path) && !File.Exists(path)) throw new FileNotFoundException("文件不存在: " + path);
        _loudGain = (float)Math.Clamp(loudGain, 0.05, 4.0);

        // 递增播放代际，防止旧播放操作覆盖新播放状态
        int gen = Interlocked.Increment(ref _playGeneration);
        _rpc.Emit("state", new { state = "loading", path });

        // ffprobe 探测不触碰输出设备，允许并发（网络流探测是耗时大头）
        var info = FfmpegPcmStream.Probe(path, headers);

        // 设备关键区串行化：多个并发 Play 排队执行，被替代的请求在入口直接退出，
        // 避免并发操作共享 backend 实例导致的流互相覆盖/设备互锁。
        lock (_playGate)
        {
            if (gen != _playGeneration) return new { ok = false, reason = "superseded" };
            StopAll(emitState: false);
            var backend = EnsureBackend();

            // Pro：DSD 原生输出分支（DoP / Native）。返回 null = 已发提示并回退普通 PCM 路径
            if (DsfReader.IsDsf(path) && _dsdMode != "pcm")
            {
                var dopResult = TryPlayDop(path, offsetSec, backend, gen);
                if (dopResult is not null) return dopResult;
            }

            // 设备释放延迟兜底：快速切歌时旧流刚释放，系统端设备句柄可能尚未完全复位
            // （USB 音频设备的独占锁释放可长达数秒），打开失败时延迟重试，总预算约 4 秒。
            for (int openRetry = 0; ; openRetry++)
            {
                if (gen != _playGeneration) return new { ok = false, reason = "superseded" };
                try
                {
                    return PlayWithBackend(path, offsetSec, headers, info, backend, gen);
                }
                catch (Exception) when (openRetry < 6 && gen == _playGeneration)
                {
                    Console.Error.WriteLine($"[engine] 打开输出设备失败，600ms 后重试 ({openRetry + 1}/6)");
                    Thread.Sleep(600);
                }
            }
        }
    }

    private object PlayWithBackend(string path, double offsetSec, string? headers, TrackInfo info, IOutputBackend backend, int gen)
    {
        try
        {
            return OpenWithChannels(path, offsetSec, headers, info, backend, gen, info.Channels);
        }
        catch (InvalidOperationException) when (info.Channels != 2)
        {
            // 通道数兜底：单声道/多声道在部分设备上不被接受时，上混/下混为立体声重试
            Console.Error.WriteLine($"[engine] {info.Channels}ch 打开失败，回退 2ch 立体声重试");
            return OpenWithChannels(path, offsetSec, headers, info, backend, gen, 2);
        }
    }

    /// <summary>Pro：解码队列容量（preload=整轨预载；否则 ≈4 秒）。</summary>
    private int CapacityFor(TrackInfo info, int rate, int channels)
        => _preload && info.DurationSec > 0
            ? (int)Math.Min((long)512 * 1024 * 1024, (long)(info.DurationSec * rate * channels * 4) + (1 << 20))
            : 0;

    private object OpenWithChannels(string path, double offsetSec, string? headers, TrackInfo info, IOutputBackend backend, int gen, int channels)
    {
        int rate = info.SampleRate;
        bool resampled = false;

        for (int attempt = 0; attempt < 2; attempt++)
        {
            var pcm = FfmpegPcmStream.Start(path, offsetSec, resampled ? rate : 0, rate, channels, headers, CapacityFor(info, rate, channels));
            var source = new PcmFloatSource(pcm, rate, channels) { Gain = _gain, LoudGain = _loudGain };
            if (_eqEnabled) { source.Eq = new EqChain(rate, channels); source.Eq.Update(_eqGains); }
            // 双声道电平：rms/peak 保留为两声道较大值（向后兼容），rmsL/peakL/rmsR/peakR 为分声道值
            source.OnLevel += (rmsL, peakL, rmsR, peakR) => _rpc.Emit("level",
                new { rms = Math.Max(rmsL, rmsR), peak = Math.Max(peakL, peakR), rmsL, peakL, rmsR, peakR });

            // Pro：crossfade 开启时经混音器输出（设备流在切歌时保持打开，同流混音过渡）
            IWaveProvider outProvider = source;
            if (_crossfadeSec > 0)
            {
                if (_mixer is null || _mixer.WaveFormat.SampleRate != rate || _mixer.Channels != channels)
                    _mixer = new CrossfadeMixer(rate, channels);
                _mixer.SetInitial(source);
                outProvider = _mixer;
            }
            else _mixer = null;

            int? needRate;
            try
            {
                needRate = backend.TryOpen(outProvider, rate, channels);
            }
            catch
            {
                // 打开失败必须释放 ffmpeg 解码进程，否则快速重试时进程泄漏
                try { pcm.Dispose(); } catch { }
                throw;
            }
            if (needRate is null)
            {
                lock (_gate)
                {
                    // 检查是否已被更新的播放请求替代
                    if (gen != _playGeneration)
                    {
                        try { pcm.Dispose(); } catch { }
                        try { backend.Stop(); } catch { }
                        return new { ok = false, reason = "superseded" };
                    }
                    _track = info; _pcm = pcm; _source = source;
                    _offsetSec = offsetSec; _decodeRate = rate; _resampled = resampled;
                    _playing = true; _ended = false; _headers = headers;
                    ApplyDspLocked(); // Pro：挂载自动前级/限幅器/响度增益
                }
                PrebufferAndStart(backend, pcm, rate, channels, gen);

                // 预缓冲后再次检查代际，防止期间被新请求替代
                lock (_gate)
                {
                    if (gen != _playGeneration)
                    {
                        try { backend.Stop(); } catch { }
                        return new { ok = false, reason = "superseded" };
                    }
                }

                _rpc.Emit("format", new
                {
                    sampleRate = rate,
                    channels,
                    requestedRate = info.SampleRate,
                    resampled,
                    codec = info.Codec,
                    bitDepth = info.BitDepth,
                    backend = backend.Kind,
                    device = backend.DeviceName,
                    outFormat = $"{backend.ActiveFormat.SampleRate}Hz/{backend.ActiveFormat.BitsPerSample}bit",
                    // Pro：Bit-perfect 直通判定（黄点原因由渲染侧展示）
                    bitPerfect = !resampled && !info.Codec.Contains("dsd", StringComparison.OrdinalIgnoreCase),
                    reason = resampled ? $"引擎重采样至 {rate}Hz"
                        : info.Codec.Contains("dsd", StringComparison.OrdinalIgnoreCase) ? "DSD 转 PCM" : "",
                    dsdMode = _dsdMode, dop = false
                });
                _rpc.Emit("state", new { state = "playing", path });
                _positionTimer.Change(0, 100);
                return new { ok = true, sampleRate = rate, resampled };
            }

            // 设备不接受该采样率 → 用 ffmpeg 重采样后重试
            pcm.Dispose();
            rate = needRate.Value;
            resampled = true;
        }
        throw new InvalidOperationException("无法以任何采样率打开输出设备。");
    }

    private void PrebufferAndStart(IOutputBackend backend, FfmpegPcmStream pcm, int rate, int channels, int gen)
    {
        // Pro：预缓冲目标跟随缓冲设置（50–500ms ×2，最少 0.3s）
        long target = (long)(rate * channels * 4 * Math.Max(0.3, _bufferMs / 1000.0 * 2));
        var sw = System.Diagnostics.Stopwatch.StartNew();
        while (!pcm.EndOfStream && !pcm.Failed && pcm.QueuedBytes < target && sw.ElapsedMilliseconds < 8000)
        {
            if (gen != _playGeneration) return; // 已被新播放请求替代：立即退出，释放关键区
            Thread.Sleep(15);
        }
        backend.Play();
    }

    /* ---------------- Pro beat0.0.1：DSD 原生输出（DoP） ---------------- */

    /// <summary>尝试以 DoP 播放 DSF；返回 null 表示已发 notify 并应回退普通 PCM 路径。</summary>
    private object? TryPlayDop(string path, double offsetSec, IOutputBackend backend, int gen)
    {
        if (_dsdMode == "native")
        {
            _rpc.Emit("notify", new { text = "Native ASIO DSD 需要驱动支持 DSD 样本格式，当前链路暂不支持，已回退转 PCM" });
            return null;
        }
        if (backend is not WasapiExclusiveBackend wb)
        {
            _rpc.Emit("notify", new { text = "DoP 仅支持 WASAPI 独占输出，已回退转 PCM" });
            return null;
        }
        DsfReader? dsf;
        try { dsf = new DsfReader(path); }
        catch { _rpc.Emit("notify", new { text = "DSF 解析失败，已回退转 PCM" }); return null; }

        if (!wb.SupportsDop(dsf.DsdRate))
        {
            _rpc.Emit("notify", new { text = $"设备不支持 {dsf.DsdRate / 16 / 1000}kHz/24bit（DoP 封装率），已回退转 PCM" });
            try { dsf.Dispose(); } catch { }
            return null;
        }
        if (offsetSec > 0.001) dsf.SeekSeconds(offsetSec);
        var dop = new DopSource(dsf);
        try { if (!wb.TryOpenDop(dop)) throw new InvalidOperationException("dop-open-rejected"); }
        catch
        {
            _rpc.Emit("notify", new { text = "DoP 打开输出失败，已回退转 PCM" });
            try { dsf.Dispose(); } catch { }
            return null;
        }

        lock (_gate)
        {
            if (gen != _playGeneration)
            {
                try { wb.Stop(); } catch { }
                try { dsf.Dispose(); } catch { }
                return new { ok = false, reason = "superseded" };
            }
            _dsf = dsf; _dopSource = dop; _dopActive = true;
            _track = new TrackInfo(path, dsf.DurationSec, dsf.DsdRate, dsf.Channels, "dsd", 1);
            _offsetSec = offsetSec; _playing = true; _ended = false;
        }
        wb.Play();
        _rpc.Emit("format", new
        {
            sampleRate = dsf.DsdRate,
            channels = dsf.Channels,
            requestedRate = dsf.DsdRate,
            resampled = false,
            codec = "dsd",
            bitDepth = 1,
            backend = backend.Kind,
            device = backend.DeviceName,
            outFormat = $"{dop.WaveFormat.SampleRate}Hz/24bit (DoP)",
            bitPerfect = true,
            reason = "",
            dsdMode = "dop",
            dop = true
        });
        _rpc.Emit("state", new { state = "playing", path });
        _positionTimer.Change(0, 100);
        return new { ok = true, dop = true };
    }

    /* ---------------- Pro beat0.0.1：交叉淡入（同流混音过渡） ---------------- */

    /// <summary>crossfade 切歌：设备流保持打开，旧曲淡出 + 新曲淡入。条件不满足时回退普通播放。</summary>
    private object PlayCrossfade(string path, string? headers, double loudGain)
    {
        if (_mixer is null || _backend is null || !_playing || _dopActive
            || DsfReader.IsDsf(path) || _crossfadeSec <= 0)
            return Play(path, 0, headers, loudGain);
        if (!FfmpegPcmStream.IsUrl(path) && !File.Exists(path)) throw new FileNotFoundException("文件不存在: " + path);

        int gen = Interlocked.Increment(ref _playGeneration);
        _rpc.Emit("state", new { state = "loading", path });
        var info = FfmpegPcmStream.Probe(path, headers);

        lock (_playGate)
        {
            if (gen != _playGeneration) return new { ok = false, reason = "superseded" };
            var mixer = _mixer!;
            int mixRate = mixer.WaveFormat.SampleRate, mixCh = mixer.Channels;
            bool resampled = info.SampleRate != mixRate;
            var pcm = FfmpegPcmStream.Start(path, 0, resampled ? mixRate : 0, mixRate, mixCh, headers, CapacityFor(info, mixRate, mixCh));
            var source = new PcmFloatSource(pcm, mixRate, mixCh) { Gain = _gain, LoudGain = (float)Math.Clamp(loudGain, 0.05, 4.0) };
            if (_eqEnabled) { source.Eq = new EqChain(mixRate, mixCh); source.Eq.Update(_eqGains); }
            source.OnLevel += (rmsL, peakL, rmsR, peakR) => _rpc.Emit("level",
                new { rms = Math.Max(rmsL, rmsR), peak = Math.Max(peakL, peakR), rmsL, peakL, rmsR, peakR });

            FfmpegPcmStream? oldPcm;
            lock (_gate)
            {
                if (gen != _playGeneration)
                {
                    try { pcm.Dispose(); } catch { }
                    return new { ok = false, reason = "superseded" };
                }
                oldPcm = _pcm;
                _pcm = pcm; _source = source; _track = info;
                _offsetSec = 0; _decodeRate = mixRate; _ended = false; _headers = headers;
                ApplyDspLocked();
            }
            mixer.FadeTo(source, _crossfadeSec);
            // 旧解码进程在淡入完成后释放（淡出期间仍在被读取）
            _ = Task.Run(async () => { await Task.Delay((int)(_crossfadeSec * 1000) + 1500); try { oldPcm?.Dispose(); } catch { } });

            _rpc.Emit("format", new
            {
                sampleRate = mixRate,
                channels = mixCh,
                requestedRate = info.SampleRate,
                resampled,
                codec = info.Codec,
                bitDepth = info.BitDepth,
                backend = _backend.Kind,
                device = _backend.DeviceName,
                outFormat = $"{_backend.ActiveFormat.SampleRate}Hz/{_backend.ActiveFormat.BitsPerSample}bit",
                bitPerfect = !resampled,
                reason = resampled ? $"引擎重采样至 {mixRate}Hz" : "",
                dsdMode = _dsdMode,
                dop = false,
                crossfade = _crossfadeSec
            });
            _rpc.Emit("state", new { state = "playing", path });
            _positionTimer.Change(0, 100);
            return new { ok = true, crossfade = _crossfadeSec };
        }
    }

    private object Pause()
    {
        lock (_gate)
        {
            if (_playing && _backend is not null)
            {
                _backend.Pause();
                _playing = false;
                _rpc.Emit("state", new { state = "paused" });
            }
        }
        return new { ok = true };
    }

    private object Resume()
    {
        lock (_gate)
        {
            if (!_playing && _backend is not null && _source is not null)
            {
                _backend.Play();
                _playing = true;
                _rpc.Emit("state", new { state = "playing" });
            }
        }
        return new { ok = true };
    }

    private object Seek(double seconds)
    {
        string? path; string? headers;
        lock (_gate) { path = _track?.Path; headers = _headers; }
        if (path is null) return new { ok = false, reason = "no-track" };
        Play(path, Math.Max(0, seconds), headers); // 复用 play 流程（-ss 输入端快seek），网络流透传 headers
        return new { ok = true };
    }

    private object StopAll(bool emitState)
    {
        lock (_gate)
        {
            _playing = false;
            _positionTimer.Change(Timeout.Infinite, Timeout.Infinite);
            // 先标记源为不活跃，让音频线程中的 Read() 感知到结束并平稳退出
            try { _source?.Deactivate(); } catch { }
            try { _dopSource?.Deactivate(); } catch { }
            try { _backend?.Stop(); } catch { }
            try { _pcm?.Dispose(); } catch { }
            try { _dsf?.Dispose(); } catch { }
            _pcm = null; _source = null; _track = null; _headers = null;
            _dsf = null; _dopSource = null; _dopActive = false; _mixer = null;
        }
        if (emitState) _rpc.Emit("state", new { state = "stopped" });
        return new { ok = true };
    }

    private void TickPosition()
    {
        double pos;
        double dur;
        bool shouldEnd = false;

        lock (_gate)
        {
            if (_track is null) return;
            if (_dopActive && _dopSource is not null)
            {
                // Pro：DoP 位置按封装帧推算（DSD 位流无电平计量）
                pos = _offsetSec + _dopSource.FramesProduced / (double)_dopSource.WaveFormat.SampleRate;
                dur = _track.DurationSec;
                if (_playing && !_ended && pos >= dur - 0.05) { _ended = true; shouldEnd = true; }
            }
            else
            {
                if (_source is null) return;
                pos = _offsetSec + (double)_source.FramesRead / _decodeRate;
                dur = _track.DurationSec;

                // 在锁内完整判断并标记结束，防止与新的 Play 调用产生竞态
                if (_playing && _source.SourceEnded && !_ended)
                {
                    _ended = true;
                    shouldEnd = true;
                }
            }
        }

        _rpc.Emit("position", new { seconds = Math.Round(pos, 3), duration = dur });

        if (shouldEnd)
        {
            // 只停止输出设备与定时器，不销毁 pcm/source（留给下一次 Play 的 StopAll 处理）
            // 这样可以避免在新 Play 已启动时误杀其资源
            lock (_gate)
            {
                _playing = false;
                _positionTimer.Change(Timeout.Infinite, Timeout.Infinite);
                try { _backend?.Stop(); } catch { }
            }
            _rpc.Emit("state", new { state = "ended" });
        }
    }
}
