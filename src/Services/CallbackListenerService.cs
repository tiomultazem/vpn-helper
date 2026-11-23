using System;
using System.IO;
using System.Net;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web;

namespace VpnHelper.Services;

public class CallbackListenerService
{
    private readonly ConfigService _config;
    private readonly LoggerService _logger;
    private readonly StateService _state;
    private HttpListener? _listener8020;
    private HttpListener? _listener8765;
    private CancellationTokenSource? _cts;

    public event Func<string, Task>? OnFortinetAuthIdReceived;
    public event Func<string, string, Task>? OnGpTokenReceived;

    public string? GpSamlHtml { get; set; }

    public CallbackListenerService(ConfigService config, LoggerService logger, StateService state)
    {
        _config = config;
        _logger = logger;
        _state = state;
    }

    public void Start()
    {
        Stop();
        _cts = new CancellationTokenSource();

        // 1. Listener Port 8020 (Fortinet SAML Callback)
        try
        {
            _listener8020 = new HttpListener();
            _listener8020.Prefixes.Add("http://127.0.0.1:8020/");
            _listener8020.Start();
            _ = Task.Run(() => Listen8020LoopAsync(_listener8020, _cts.Token));
            _logger.AddLog("Listener callback SSO Fortinet aktif di http://127.0.0.1:8020/", "info", userVisible: false);
        }
        catch (Exception ex)
        {
            _logger.AddLog($"Gagal mengaktifkan listener port 8020: {ex.Message}", "warning");
        }

        // 2. Listener Port 8765 (GlobalProtect SAML Form & Extension Callback)
        try
        {
            _listener8765 = new HttpListener();
            _listener8765.Prefixes.Add("http://127.0.0.1:8765/");
            _listener8765.Start();
            _ = Task.Run(() => Listen8765LoopAsync(_listener8765, _cts.Token));
            _logger.AddLog("Listener callback GlobalProtect aktif di http://127.0.0.1:8765/", "info", userVisible: false);
        }
        catch (Exception ex)
        {
            _logger.AddLog($"Gagal mengaktifkan listener port 8765: {ex.Message}", "warning");
        }
    }

    private async Task Listen8020LoopAsync(HttpListener listener, CancellationToken ct)
    {
        while (!ct.IsCancellationRequested && listener.IsListening)
        {
            try
            {
                var context = await listener.GetContextAsync();
                var req = context.Request;
                var res = context.Response;

                var query = HttpUtility.ParseQueryString(req.Url?.Query ?? "");
                var authId = query["id"];

                if (!string.IsNullOrEmpty(authId))
                {
                    _logger.AddLog("Authentication ID Fortinet diterima dari browser.", "success");
                    _ = Task.Run(async () =>
                    {
                        if (OnFortinetAuthIdReceived != null)
                        {
                            await OnFortinetAuthIdReceived.Invoke(authId);
                        }
                    });

                    var html = @"<!DOCTYPE html>
<html lang=""id"">
<head><meta charset=""UTF-8""><title>Login Berhasil</title>
<style>body{background:#0f172a;color:#e2e8f0;font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;}
.card{background:#1e293b;padding:32px;border-radius:12px;text-align:center;box-shadow:0 10px 30px rgba(0,0,0,.5);}</style>
</head>
<body><div class=""card""><h2>Login Berhasil!</h2><p>Authentication ID telah diterima. Silakan kembali ke aplikasi.</p></div>
<script>setTimeout(function(){window.close();}, 2000);</script>
</body></html>";
                    var buffer = Encoding.UTF8.GetBytes(html);
                    res.ContentType = "text/html; charset=utf-8";
                    res.ContentLength64 = buffer.Length;
                    await res.OutputStream.WriteAsync(buffer, ct);
                }
                else
                {
                    var ping = Encoding.UTF8.GetBytes("VPN Helper Listener OK");
                    res.ContentLength64 = ping.Length;
                    await res.OutputStream.WriteAsync(ping, ct);
                }
                res.Close();
            }
            catch { }
        }
    }

    private async Task Listen8765LoopAsync(HttpListener listener, CancellationToken ct)
    {
        while (!ct.IsCancellationRequested && listener.IsListening)
        {
            try
            {
                var context = await listener.GetContextAsync();
                var req = context.Request;
                var res = context.Response;
                var path = req.Url?.AbsolutePath ?? "";

                // Sajikan Form SAML untuk submit otomatis ke IdP di browser
                if (path.Equals("/gp/saml-form", StringComparison.OrdinalIgnoreCase))
                {
                    var html = GpSamlHtml ?? "SAML form belum siap.";
                    if (!html.Contains("myform.submit()"))
                    {
                        html = html.Replace("</body>", "<script>document.getElementById('myform').submit();</script></body>");
                    }
                    var buffer = Encoding.UTF8.GetBytes(html);
                    res.ContentType = "text/html; charset=utf-8";
                    res.ContentLength64 = buffer.Length;
                    await res.OutputStream.WriteAsync(buffer, ct);
                    res.Close();
                    continue;
                }

                // Terima token dari Extension / Userscript browser eksternal
                if (path.Equals("/api/gp/callback", StringComparison.OrdinalIgnoreCase) && req.HttpMethod == "POST")
                {
                    using var reader = new StreamReader(req.InputStream, req.ContentEncoding);
                    var body = await reader.ReadToEndAsync(ct);
                    var parsed = HttpUtility.ParseQueryString(body);

                    var token = parsed["token"] ?? "";
                    var username = parsed["username"] ?? _config.SsoUsername;

                    _logger.AddLog("Token GlobalProtect diterima dari Browser/Extension.", "success");

                    _ = Task.Run(async () =>
                    {
                        if (OnGpTokenReceived != null && !string.IsNullOrEmpty(token))
                        {
                            await OnGpTokenReceived.Invoke(token, username);
                        }
                    });

                    var json = "{\"success\":true}";
                    var buffer = Encoding.UTF8.GetBytes(json);
                    res.ContentType = "application/json";
                    res.ContentLength64 = buffer.Length;
                    await res.OutputStream.WriteAsync(buffer, ct);
                    res.Close();
                    continue;
                }

                res.StatusCode = 404;
                res.Close();
            }
            catch { }
        }
    }

    public void Stop()
    {
        _cts?.Cancel();
        try { _listener8020?.Stop(); } catch { }
        try { _listener8765?.Stop(); } catch { }
        _listener8020 = null;
        _listener8765 = null;
    }
}
