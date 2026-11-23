using System;
using System.Collections.Concurrent;
using System.Collections.ObjectModel;
using System.IO;
using System.Linq;
using System.Text;
using System.Windows;
using VpnHelper.Models;

namespace VpnHelper.Services;

public class LoggerService
{
    private readonly string _logFilePath;
    private readonly object _fileLock = new();
    private const int MaxEntries = 500;
    private const long MaxFileSizeBytes = 200 * 1024; // 200 KB

    public ObservableCollection<LogEntry> Logs { get; } = new();
    public event Action<LogEntry>? LogAdded;

    public LoggerService(ConfigService config)
    {
        _logFilePath = Path.Combine(config.BaseDirectory, "vpn_helper.log");
        LoadPersistedLogs();
    }

    private void LoadPersistedLogs()
    {
        if (!File.Exists(_logFilePath)) return;
        try
        {
            var lines = File.ReadLines(_logFilePath).TakeLast(MaxEntries);
            foreach (var line in lines)
            {
                var match = System.Text.RegularExpressions.Regex.Match(line.Trim(), @"^\[(.*?)\] \[(.*?)\] (.*)$");
                if (match.Success)
                {
                    var fullTs = match.Groups[1].Value;
                    var lvl = match.Groups[2].Value.ToLowerInvariant();
                    var msg = match.Groups[3].Value;
                    if (msg.StartsWith("[UI] "))
                    {
                        var cleanMsg = msg[5..];
                        var shortTs = fullTs.Length >= 19 ? fullTs[5..] : fullTs;
                        Logs.Add(new LogEntry(shortTs, cleanMsg, lvl));
                    }
                }
            }
        }
        catch { }
    }

    public void AddLog(string message, string level = "info", bool userVisible = true)
    {
        var now = DateTime.Now;
        var shortTs = now.ToString("MM-dd HH:mm:ss");
        var fullTs = now.ToString("yyyy-MM-dd HH:mm:ss");

        // Tulis ke debug output
        System.Diagnostics.Debug.WriteLine($"[{now:HH:mm:ss}] [{level.ToUpper()}] {message}");

        // Tulis ke file vpn_helper.log dengan rotasi 200 KB
        lock (_fileLock)
        {
            try
            {
                var fileInfo = new FileInfo(_logFilePath);
                if (fileInfo.Exists && fileInfo.Length > MaxFileSizeBytes)
                {
                    File.WriteAllText(_logFilePath, $"[{fullTs}] [INFO] Log dikosongkan otomatis (> 200KB).\n", Encoding.UTF8);
                }

                var line = $"[{fullTs}] [{level.ToUpper()}] {(userVisible ? "[UI] " : "")}{message}\n";
                File.AppendAllText(_logFilePath, line, Encoding.UTF8);
            }
            catch
            {
                // Abaikan kesalahan I/O berkas log
            }
        }

        if (!userVisible)
        {
            return;
        }

        var entry = new LogEntry(shortTs, message, level.ToLowerInvariant());

        if (System.Windows.Application.Current != null)
        {
            System.Windows.Application.Current.Dispatcher.InvokeAsync(() =>
            {
                Logs.Add(entry);
                if (Logs.Count > MaxEntries)
                {
                    Logs.RemoveAt(0);
                }
                LogAdded?.Invoke(entry);
            });
        }
        else
        {
            Logs.Add(entry);
            LogAdded?.Invoke(entry);
        }
    }

    public void ClearLogs()
    {
        lock (_fileLock)
        {
            try
            {
                File.WriteAllText(_logFilePath, string.Empty, Encoding.UTF8);
            }
            catch { }
        }

        System.Windows.Application.Current?.Dispatcher.InvokeAsync(() =>
        {
            Logs.Clear();
            AddLog("Log dibersihkan.", "info");
        });
    }
}
