param(
  [Parameter(Mandatory = $true)]
  [ValidateSet("set", "assert", "restore")]
  [string]$Mode
)

$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force -Path logs | Out-Null

Add-Type @"
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class Pr8047Native {
  [StructLayout(LayoutKind.Sequential)]
  public struct RECT { public int Left, Top, Right, Bottom; }
  public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll", SetLastError=true)]
  public static extern bool SystemParametersInfo(uint action, uint param, ref RECT rect, uint flags);
  [DllImport("user32.dll")]
  public static extern bool EnumWindows(EnumWindowsProc callback, IntPtr lParam);
  [DllImport("user32.dll")]
  public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)]
  public static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int count);
  [DllImport("user32.dll")]
  public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
  [DllImport("user32.dll")]
  public static extern bool GetClientRect(IntPtr hWnd, out RECT rect);
}
"@

function Get-WorkArea {
  $rect = New-Object Pr8047Native+RECT
  if (-not [Pr8047Native]::SystemParametersInfo(0x0030, 0, [ref]$rect, 0)) {
    throw "SPI_GETWORKAREA failed: $([Runtime.InteropServices.Marshal]::GetLastWin32Error())"
  }
  return $rect
}

function Set-WorkArea([int]$Left, [int]$Top, [int]$Right, [int]$Bottom) {
  $rect = New-Object Pr8047Native+RECT
  $rect.Left = $Left; $rect.Top = $Top; $rect.Right = $Right; $rect.Bottom = $Bottom
  if (-not [Pr8047Native]::SystemParametersInfo(0x002F, 0, [ref]$rect, 0x0002)) {
    throw "SPI_SETWORKAREA failed: $([Runtime.InteropServices.Marshal]::GetLastWin32Error())"
  }
}

if ($Mode -eq "set") {
  $original = Get-WorkArea
  @{
    Left = $original.Left; Top = $original.Top
    Right = $original.Right; Bottom = $original.Bottom
  } | ConvertTo-Json | Set-Content logs/original-workarea.json
  $right = [Math]::Min($original.Right, 1000)
  $bottom = [Math]::Min($original.Bottom, 500)
  if (($right - $original.Left) -lt 800 -or ($bottom - $original.Top) -lt 400) {
    throw "runner work area is too small for the compact test: $($original.Right)x$($original.Bottom)"
  }
  Set-WorkArea $original.Left $original.Top $right $bottom
  $target = Get-WorkArea
  @{
    Left = $target.Left; Top = $target.Top
    Right = $target.Right; Bottom = $target.Bottom
  } | ConvertTo-Json | Set-Content logs/target-workarea.json
  Write-Host "PASS compact Windows work area set to $($target.Right-$target.Left)x$($target.Bottom-$target.Top)@$($target.Left),$($target.Top)"
  exit 0
}

if ($Mode -eq "restore") {
  if (Test-Path logs/original-workarea.json) {
    $saved = Get-Content logs/original-workarea.json | ConvertFrom-Json
    Set-WorkArea $saved.Left $saved.Top $saved.Right $saved.Bottom
    Write-Host "PASS original Windows work area restored"
  }
  exit 0
}

$script:FoundHwnd = [IntPtr]::Zero
$deadline = [DateTime]::UtcNow.AddMinutes(5)
while ([DateTime]::UtcNow -lt $deadline -and $script:FoundHwnd -eq [IntPtr]::Zero) {
  $callback = [Pr8047Native+EnumWindowsProc]{
    param([IntPtr]$hWnd, [IntPtr]$lParam)
    if (-not [Pr8047Native]::IsWindowVisible($hWnd)) { return $true }
    $text = New-Object Text.StringBuilder 512
    [void][Pr8047Native]::GetWindowText($hWnd, $text, $text.Capacity)
    if ($text.ToString() -eq "Unsloth") {
      $script:FoundHwnd = $hWnd
      return $false
    }
    return $true
  }
  [void][Pr8047Native]::EnumWindows($callback, [IntPtr]::Zero)
  if ($script:FoundHwnd -eq [IntPtr]::Zero) { Start-Sleep -Seconds 1 }
}
if ($script:FoundHwnd -eq [IntPtr]::Zero) { throw "Unsloth window did not appear within five minutes" }

$outer = New-Object Pr8047Native+RECT
$client = New-Object Pr8047Native+RECT
if (-not [Pr8047Native]::GetWindowRect($script:FoundHwnd, [ref]$outer)) { throw "GetWindowRect failed" }
if (-not [Pr8047Native]::GetClientRect($script:FoundHwnd, [ref]$client)) { throw "GetClientRect failed" }
$work = Get-WorkArea
$geometry = @{
  x = $outer.Left; y = $outer.Top
  width = $outer.Right - $outer.Left; height = $outer.Bottom - $outer.Top
  client_width = $client.Right - $client.Left; client_height = $client.Bottom - $client.Top
  frame_width = ($outer.Right - $outer.Left) - ($client.Right - $client.Left)
  frame_height = ($outer.Bottom - $outer.Top) - ($client.Bottom - $client.Top)
  work_x = $work.Left; work_y = $work.Top
  work_width = $work.Right - $work.Left; work_height = $work.Bottom - $work.Top
}
$geometry | ConvertTo-Json | Set-Content logs/windows-geometry.json

if ($geometry.frame_width -le 0 -and $geometry.frame_height -le 0) {
  throw "FAIL live Windows window exposed no outer/client frame delta: $($geometry | ConvertTo-Json -Compress)"
}
$slack = 1
if ($outer.Left -lt ($work.Left - $slack) -or $outer.Top -lt ($work.Top - $slack) -or
    $outer.Right -gt ($work.Right + $slack) -or $outer.Bottom -gt ($work.Bottom + $slack)) {
  throw "FAIL live Windows outer window escapes work area: $($geometry | ConvertTo-Json -Compress)"
}

try {
  Add-Type -AssemblyName System.Drawing
  $bitmap = New-Object Drawing.Bitmap ($work.Right-$work.Left), ($work.Bottom-$work.Top)
  $graphics = [Drawing.Graphics]::FromImage($bitmap)
  $graphics.CopyFromScreen($work.Left, $work.Top, 0, 0, $bitmap.Size)
  $bitmap.Save((Join-Path $PWD "logs/windows-desktop.png"), [Drawing.Imaging.ImageFormat]::Png)
  $graphics.Dispose(); $bitmap.Dispose()
} catch {
  Write-Warning "Geometry passed but screenshot capture was unavailable: $_"
}
Write-Host "PASS live Windows outer window fits work area: outer=$($geometry.width)x$($geometry.height)@$($geometry.x),$($geometry.y) frame=$($geometry.frame_width)x$($geometry.frame_height) work=$($geometry.work_width)x$($geometry.work_height)"
