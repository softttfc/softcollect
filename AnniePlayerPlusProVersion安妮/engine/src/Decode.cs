using System.Collections.Concurrent;
using System.Diagnostics;
using System.Text.Json;

namespace MineEngine;

/// <summary>曲目元数据（ffprobe 探测结果）。</summary>
public sealed record TrackInfo(
    string Path,
    double DurationSec,
    int SampleRate,
    int Channels,
    string Codec,
    int BitDepth);

/// <summary>
/// FFmpeg 解码管道：把任意音频解码为 float32le 交错 PCM，喂入带背压的块队列。
/// 队列容量按字节数限制（约 4 秒音频），防止长曲目全量解码撑爆内存。
/// </summary>
public sealed class FfmpegPcmStream : IDisposable
{
    public const int ChunkBytes = 1 << 16; // 64KB / 块

    private readonly BlockingCollection<byte[]> _queue;
    private readonly Process _proc;
    private readonly Task _reader;
    private volatile bool _eof;
    private volatile bool _failed;
    private long _queuedBytes;

    public bool EndOfStream => _eof && _queue.Count == 0;
    public bool Failed => _failed;
    public long QueuedBytes => System.Threading.Interlocked.Read(ref _queuedBytes);

    public static string FfmpegPath = "ffmpeg";
    public static string FfprobePath = "ffprobe";

    /// <summary>判断输入是否为网络流地址。</summary>
    public static bool IsUrl(string p) => p.StartsWith("http://") || p.StartsWith("https://");

    private FfmpegPcmStream(Process proc, int capacityBytes)
    {
        _proc = proc;
        _queue = new BlockingCollection<byte[]>(new ConcurrentQueue<byte[]>(), Math.Max(2, capacityBytes / ChunkBytes));
        _reader = Task.Run(ReadLoop);
    }

    /// <summary>启动解码。offsetSec &gt; 0 时使用输入端 -ss 快seek；resampleRate &gt; 0 时让 ffmpeg 重采样。headers 为可选 HTTP 头（如 Referer），仅对 URL 生效。capacityBytes &gt; 0 时覆盖默认队列容量（Pro：整轨预载）。</summary>
    public static FfmpegPcmStream Start(string path, double offsetSec, int resampleRate, int sampleRate, int channels, string? headers = null, int capacityBytes = 0)
    {
        int capBytes = capacityBytes > 0 ? capacityBytes : Math.Max(ChunkBytes * 4, sampleRate * channels * 4 * 4); // 默认 ≈4 秒
        var args = new List<string> { "-hide_banner", "-v", "error", "-nostdin" };
        if (IsUrl(path))
        {
            // 网络流：断线自动重连 + I/O 超时保护 + 平台要求的 Referer 等头
            args.Add("-reconnect"); args.Add("1");
            args.Add("-reconnect_streamed"); args.Add("1");
            args.Add("-reconnect_delay_max"); args.Add("5");
            args.Add("-rw_timeout"); args.Add("15000000"); // 15 秒（微秒）
            if (!string.IsNullOrWhiteSpace(headers)) { args.Add("-headers"); args.Add(headers); }
        }
        if (offsetSec > 0.001) { args.Add("-ss"); args.Add(offsetSec.ToString("0.###", System.Globalization.CultureInfo.InvariantCulture)); }
        args.Add("-i"); args.Add(path);
        if (resampleRate > 0) { args.Add("-ar"); args.Add(resampleRate.ToString()); }
        args.Add("-ac"); args.Add(channels.ToString()); // 通道数对齐（单声道可上混立体声）
        args.Add("-f"); args.Add("f32le");
        args.Add("-acodec"); args.Add("pcm_f32le");
        args.Add("pipe:1");

        var psi = new ProcessStartInfo(FfmpegPath, string.Join(' ', args.Select(Quote)))
        {
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        var proc = Process.Start(psi) ?? throw new InvalidOperationException("无法启动 ffmpeg，请确认 ffmpeg.exe 在 PATH 或 engine/tools 下。");
        var stream = new FfmpegPcmStream(proc, capBytes);
        // 异步吞掉 stderr，避免管道阻塞
        _ = Task.Run(async () =>
        {
            var err = await proc.StandardError.ReadToEndAsync();
            if (!string.IsNullOrWhiteSpace(err)) Console.Error.WriteLine("[ffmpeg] " + err.Trim());
        });
        return stream;
    }

    private static string Quote(string s) => s.Contains(' ') || s.Contains('"') ? '"' + s.Replace("\"", "\\\"") + '"' : s;

    private void ReadLoop()
    {
        try
        {
            var stdout = _proc.StandardOutput.BaseStream;
            while (true)
            {
                var buf = new byte[ChunkBytes];
                int filled = 0;
                while (filled < buf.Length)
                {
                    int n = stdout.Read(buf, filled, buf.Length - filled);
                    if (n <= 0) break;
                    filled += n;
                }
                if (filled == 0) break;
                if (filled < buf.Length) Array.Resize(ref buf, filled);
                _queue.Add(buf); // 满时阻塞 = 背压
                System.Threading.Interlocked.Add(ref _queuedBytes, buf.Length);
            }
        }
        catch (Exception ex)
        {
            _failed = true;
            Console.Error.WriteLine("[decode] " + ex.Message);
        }
        finally
        {
            _eof = true;
            try { _queue.CompleteAdding(); } catch { }
        }
    }

    /// <summary>取一块 PCM；无数据但未到结尾时阻塞等待（带超时）。返回 null 表示暂时无数据或流结束。</summary>
    public byte[]? Take(int timeoutMs)
    {
        try
        {
            if (_queue.TryTake(out var buf, timeoutMs))
            {
                System.Threading.Interlocked.Add(ref _queuedBytes, -buf.Length);
                return buf;
            }
            return null;
        }
        catch (InvalidOperationException) { return null; } // CompleteAdding 后空队列
    }

    /// <summary>探测曲目信息。headers 为可选 HTTP 头，仅对 URL 生效。</summary>
    public static TrackInfo Probe(string path, string? headers = null)
    {
        var argList = new List<string> { "-v", "error" };
        if (IsUrl(path))
        {
            argList.Add("-timeout"); argList.Add("15000000"); // 15 秒（微秒）
            if (!string.IsNullOrWhiteSpace(headers)) { argList.Add("-headers"); argList.Add(headers); }
        }
        argList.Add("-show_entries"); argList.Add("stream=codec_name,sample_rate,channels,bits_per_sample:format=duration");
        argList.Add("-of"); argList.Add("json");
        argList.Add(path);

        var psi = new ProcessStartInfo(FfprobePath, string.Join(' ', argList.Select(Quote)))
        {
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        using var proc = Process.Start(psi) ?? throw new InvalidOperationException("无法启动 ffprobe。");
        var json = proc.StandardOutput.ReadToEnd();
        proc.WaitForExit(IsUrl(path) ? 20000 : 10000); // 网络流探测允许更长等待

        using var doc = JsonDocument.Parse(json);
        var root = doc.RootElement;
        var stream = root.GetProperty("streams").EnumerateArray()
            .FirstOrDefault(s => s.TryGetProperty("sample_rate", out _));
        if (stream.ValueKind == JsonValueKind.Undefined) throw new InvalidOperationException("ffprobe 未找到音频流: " + path);

        int rate = int.Parse(stream.GetProperty("sample_rate").GetString() ?? "0");
        int ch = stream.TryGetProperty("channels", out var chEl) ? chEl.GetInt32() : 2;
        int bits = stream.TryGetProperty("bits_per_sample", out var bEl) && bEl.ValueKind == JsonValueKind.Number ? bEl.GetInt32() : 0;
        string codec = stream.TryGetProperty("codec_name", out var cEl) ? cEl.GetString() ?? "?" : "?";
        double dur = 0;
        if (root.TryGetProperty("format", out var fmt) && fmt.TryGetProperty("duration", out var dEl))
            double.TryParse(dEl.GetString(), System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out dur);
        return new TrackInfo(path, dur, rate, ch, codec, bits);
    }

    public void Dispose()
    {
        try { if (!_proc.HasExited) _proc.Kill(entireProcessTree: true); } catch { }
        try { _proc.Dispose(); } catch { }
    }
}
