# 截取屏幕到指定路径，并尝试聚焦给定窗口标题
param(
    [Parameter(Mandatory=$true)][string]$OutPath,
    [string]$WindowTitle = "",
    [int]$DelaySec = 2
)

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

if ($WindowTitle -ne "") {
    Add-Type @"
using System;
using System.Runtime.InteropServices;
public class Win {
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll", CharSet=CharSet.Auto)] public static extern IntPtr FindWindow(string c, string n);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int n);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [StructLayout(LayoutKind.Sequential)]
  public struct RECT { public int Left, Top, Right, Bottom; }
}
"@
    Start-Sleep -Milliseconds 300
    $h = [Win]::FindWindow($null, $WindowTitle)
    if ($h -ne [IntPtr]::Zero) {
        [void][Win]::ShowWindow($h, 9)  # SW_RESTORE
        [void][Win]::SetForegroundWindow($h)
    } else {
        Write-Host "[WARN] window not found: $WindowTitle"
    }
}

Start-Sleep -Seconds $DelaySec

$screen = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$bmp = New-Object System.Drawing.Bitmap $screen.Width, $screen.Height
$gfx = [System.Drawing.Graphics]::FromImage($bmp)
$gfx.CopyFromScreen($screen.Location, [System.Drawing.Point]::Empty, $screen.Size)

# 若指定窗口找到，则裁剪到窗口区域
if ($WindowTitle -ne "" -and $h -ne [IntPtr]::Zero) {
    $rect = New-Object Win+RECT
    [void][Win]::GetWindowRect($h, [ref]$rect)
    $w = $rect.Right - $rect.Left
    $he = $rect.Bottom - $rect.Top
    if ($w -gt 50 -and $he -gt 50) {
        $crop = New-Object System.Drawing.Bitmap $w, $he
        $cg = [System.Drawing.Graphics]::FromImage($crop)
        $cg.DrawImage($bmp, 0, 0, (New-Object System.Drawing.Rectangle $rect.Left, $rect.Top, $w, $he), [System.Drawing.GraphicsUnit]::Pixel)
        $cg.Dispose()
        $bmp.Dispose()
        $bmp = $crop
    }
}

$dir = Split-Path -Parent $OutPath
if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
$bmp.Save($OutPath, [System.Drawing.Imaging.ImageFormat]::Png)
$gfx.Dispose()
$bmp.Dispose()
Write-Host "[OK] saved: $OutPath"
