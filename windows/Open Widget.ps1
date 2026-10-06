# Opens CAF Quick View as a small always-on-top window in Edge.
$ErrorActionPreference = "Stop"
$boot = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString("yyyyMMddHHmmss")
$url = "https://koffeekinggamer.github.io/CAF-Quick-View/widget.html#boot=$boot"

$pf86 = [Environment]::GetEnvironmentVariable("ProgramFiles(x86)")
$candidates = @(
  "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
  "$pf86\Microsoft\Edge\Application\msedge.exe",
  "$env:LocalAppData\Microsoft\Edge\Application\msedge.exe",
  "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
  "$env:LocalAppData\Google\Chrome\Application\chrome.exe"
)
$browser = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $browser) {
  Add-Type -AssemblyName System.Windows.Forms
  [System.Windows.Forms.MessageBox]::Show("Microsoft Edge is not installed. Install Edge, then open this again.")
  exit 1
}

$profile = Join-Path $env:LocalAppData "CAF Quick View\edge-profile"
New-Item -ItemType Directory -Force -Path $profile | Out-Null

Start-Process -FilePath $browser -ArgumentList @(
  "--app=$url",
  "--user-data-dir=$profile",
  "--window-size=380,640",
  "--window-position=48,48",
  "--no-first-run",
  "--disable-extensions"
)

Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class WidgetWin {
  delegate bool EnumProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc lpEnumFunc, IntPtr lParam);
  [DllImport("user32.dll")] static extern int GetWindowText(IntPtr hWnd, StringBuilder lpString, int nMaxCount);
  [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll")] static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint uFlags);
  public static IntPtr Find(string part) {
    IntPtr found = IntPtr.Zero;
    EnumWindows((h, l) => {
      if (!IsWindowVisible(h)) return true;
      var sb = new StringBuilder(512);
      GetWindowText(h, sb, sb.Capacity);
      if (sb.ToString().IndexOf(part, StringComparison.OrdinalIgnoreCase) >= 0) {
        found = h;
        return false;
      }
      return true;
    }, IntPtr.Zero);
    return found;
  }
  public static void KeepOnTop(IntPtr hwnd) {
    SetWindowPos(hwnd, new IntPtr(-1), 0, 0, 0, 0, 0x0001 | 0x0002);
  }
}
"@

for ($i = 0; $i -lt 40; $i++) {
  $hwnd = [WidgetWin]::Find("CAF Quick View")
  if ($hwnd -ne [IntPtr]::Zero) {
    [WidgetWin]::KeepOnTop($hwnd)
    break
  }
  Start-Sleep -Milliseconds 400
}
