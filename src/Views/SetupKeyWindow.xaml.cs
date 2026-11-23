using System.Windows;
using VpnHelper.Services;

namespace VpnHelper.Views;

public partial class SetupKeyWindow : Window
{
    private readonly string _baseDir;

    public SetupKeyWindow(string baseDir)
    {
        InitializeComponent();
        _baseDir = baseDir;
        Loaded += (s, e) => KeyTextBox.Focus();
    }

    private void SubmitButton_Click(object sender, RoutedEventArgs e)
    {
        var answer = KeyTextBox.Text.Trim();
        if (string.IsNullOrEmpty(answer))
        {
            ShowError("Jawaban tidak boleh kosong.");
            return;
        }

        if (FernetDecryptor.TryDecryptEnvFile(answer, _baseDir, out var error))
        {
            DialogResult = true;
            Close();
        }
        else
        {
            ShowError(error);
        }
    }

    private void CancelButton_Click(object sender, RoutedEventArgs e)
    {
        DialogResult = false;
        Close();
    }

    private void ShowError(string message)
    {
        ErrorTextBlock.Text = message;
        ErrorTextBlock.Visibility = Visibility.Visible;
    }
}