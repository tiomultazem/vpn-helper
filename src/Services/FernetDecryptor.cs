using System;
using System.IO;
using System.Security.Cryptography;
using System.Text;

namespace VpnHelper.Services;

public static class FernetDecryptor
{
#if MITRA_BUILD
    private const string MitraInternalSecret = "VpnHelper.Mitra.InternalSecret.2026";

    /// <summary>
    /// Mendekripsi konfigurasi Mitra yang tersimpan dalam format terenkripsi AES-256 (RAM only).
    /// </summary>
    public static string GetMitraDecryptedEnv()
    {
        if (string.IsNullOrEmpty(MitraPayload.EncryptedBase64))
        {
            return string.Empty;
        }

        try
        {
            var combined = Convert.FromBase64String(MitraPayload.EncryptedBase64);
            if (combined.Length < 32)
            {
                return string.Empty;
            }

            var key = SHA256.HashData(Encoding.UTF8.GetBytes(MitraInternalSecret));
            var iv = combined.AsSpan(0, 16).ToArray();
            var cipherOffset = 16;
            var cipherLength = combined.Length - 16;

            using var aes = Aes.Create();
            aes.Key = key;
            aes.IV = iv;
            aes.Mode = CipherMode.CBC;
            aes.Padding = PaddingMode.PKCS7;

            using var decryptor = aes.CreateDecryptor();
            var decryptedBytes = decryptor.TransformFinalBlock(combined, cipherOffset, cipherLength);
            return Encoding.UTF8.GetString(decryptedBytes);
        }
        catch
        {
            return string.Empty;
        }
    }
#endif

    /// <summary>
    /// Digunakan pada versi Production untuk mendekripsi berkas .env.enc atau men-generate .env bersih
    /// setelah pengguna menjawab pertanyaan sestama ("zulkipli").
    /// </summary>
    public static bool TryDecryptEnvFile(string answer, string baseDir, out string errorMessage)
    {
        errorMessage = string.Empty;
        var envPath = Path.Combine(baseDir, ".env");
        var encPath = Path.Combine(baseDir, ".env.enc");

        var cleanAnswer = answer.Trim().ToLowerInvariant();

        // 1. Jika ada berkas fisik .env.enc, dekripsi menggunakan format Fernet (SHA256 dari jawaban)
        if (File.Exists(encPath))
        {
            try
            {
                var tokenString = File.ReadAllText(encPath, Encoding.UTF8).Trim();
                var tokenBytes = Base64UrlDecode(tokenString);

                if (tokenBytes.Length >= 57 && tokenBytes[0] == 0x80)
                {
                    var sha256 = SHA256.HashData(Encoding.UTF8.GetBytes(cleanAnswer));
                    var signingKey = sha256.AsSpan(0, 16).ToArray();
                    var encryptionKey = sha256.AsSpan(16, 16).ToArray();

                    var payloadLength = tokenBytes.Length - 32;
                    using var hmac = new HMACSHA256(signingKey);
                    var computedHmac = hmac.ComputeHash(tokenBytes, 0, payloadLength);
                    var expectedHmac = tokenBytes.AsSpan(payloadLength, 32);

                    if (CryptographicOperations.FixedTimeEquals(computedHmac, expectedHmac))
                    {
                        var iv = tokenBytes.AsSpan(9, 16).ToArray();
                        var ciphertextOffset = 25;
                        var cipherLength = payloadLength - ciphertextOffset;

                        using var aes = Aes.Create();
                        aes.Key = encryptionKey;
                        aes.IV = iv;
                        aes.Mode = CipherMode.CBC;
                        aes.Padding = PaddingMode.PKCS7;

                        using var decryptor = aes.CreateDecryptor();
                        var decryptedBytes = decryptor.TransformFinalBlock(tokenBytes, ciphertextOffset, cipherLength);

                        File.WriteAllBytes(envPath, decryptedBytes);
                        return true;
                    }
                }
            }
            catch { }
        }

        // 2. Jika tidak ada .env.enc, verifikasi kata kunci "zulkipli" / "zulkifli"
        if (cleanAnswer != "zulkipli" && cleanAnswer != "zulkifli")
        {
            errorMessage = "Jawaban salah! Gagal mengonfirmasi otorisasi.";
            return false;
        }

        // 3. Generate template berkas .env bersih untuk Production (kredensial kosong diisi manual oleh user)
        try
        {
            var templateContent = GetCleanProductionEnvTemplate();
            File.WriteAllText(envPath, templateContent, Encoding.UTF8);
            return true;
        }
        catch (Exception ex)
        {
            errorMessage = $"Gagal membuat file .env: {ex.Message}";
            return false;
        }
    }

    private static string GetCleanProductionEnvTemplate()
    {
        return "SSO_USERNAME=\"masukin sso lu dan hapus petik\"\n" +
               "SSO_PASSWORD=\"masukin pass lu dan hapus petik\"\n" +
               "app_host=127.0.0.1\n" +
               "app_port=8765\n" +
               "app_debug=false\n" +
               "callback_host=127.0.0.1\n" +
               "callback_port=8020\n" +
               "callback_debug=false\n" +
               "use_reloader=false\n" +
               "vpn_scheme=https\n" +
               "vpn_host=akses.bps.go.id\n" +
               "gp_host=vpn.bps.go.id\n" +
               "gp_gateway=vpn-gw01.bps.go.id\n" +
               "gp_port=443\n" +
               "gp_protocol=gp\n" +
               "gp_gateway_prelogin_path=/ssl-vpn/prelogin.esp\n" +
               "gp_portal_prelogin_path=/global-protect/prelogin.esp\n" +
               "gp_session_cookie=SESSID\n" +
               "vpn_port=443\n" +
               "verify_tls=true\n" +
               "request_timeout=25\n" +
               "saml_start_path=/remote/saml/start\n" +
               "saml_start_query=redirect=1\n" +
               "saml_auth_id_path=/remote/saml/auth_id\n" +
               "saml_auth_id_param=id\n" +
               "vpn_config_path=/remote/fortisslvpn_xml\n" +
               "vpn_config_query=dual_stack=1\n" +
               "session_cookie_name=SVPNCOOKIE\n" +
               "http_user_agent=Mozilla/5.0 SV1\n" +
               "http_accept=*/*\n" +
               "http_pragma=no-cache\n" +
               "openconnect_path=C:\\Program Files\\OpenConnect-GUI\\openconnect.exe\n" +
               "openconnect_binary_names=openconnect;openconnect.exe\n" +
               "openconnect_search_paths=C:\\Program Files\\OpenConnect-GUI\\openconnect.exe;C:\\Program Files (x86)\\OpenConnect-GUI\\openconnect.exe\n" +
               "openconnect_protocol=fortinet\n" +
               "tunnel_interface=VPN Helper\n" +
               "openconnect_auto_no_dtls=true\n" +
               "openconnect_extra_args=--force-dpd=30\n" +
               "ping_target=10.10.11.11\n" +
               "ping_interval=30\n" +
               "log_max_entries=500\n" +
               "status_log_limit=80\n" +
               "mask_keep_start=6\n" +
               "mask_keep_end=4\n" +
               "enable_tray=true\n" +
               "auto_hide_console=true\n" +
               "traktir_wishlist=yaAllah-2026-pengen-mesin-espresso-gratis-tanpa-mengurangi-jatah-rezekiku-yang-lain-di-tahun-ini-aamiin\n" +
               "dev=kopken\n" +
               "automate=false\n" +
               "bypass_enabled=true\n" +
               "bypass_domains=rs-ny.rustdesk.com;https://d21.team;*.google.com;*.youtube.com;*.cloudflare.com;*.github.com\n";
    }

    private static byte[] Base64UrlDecode(string input)
    {
        var output = input.Trim().Replace('-', '+').Replace('_', '/');
        switch (output.Length % 4)
        {
            case 2: output += "=="; break;
            case 3: output += "="; break;
        }
        return Convert.FromBase64String(output);
    }
}
