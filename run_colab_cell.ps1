Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, System.Windows.Forms

# Win32 functions for window foreground
$code = @"
using System;
using System.Runtime.InteropServices;
public class Win32 {
    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")]
    public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")]
    public static extern bool BringWindowToTop(IntPtr hWnd);
}
"@
Add-Type -TypeDefinition $code

$chrome = Get-Process -Name chrome -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowTitle -like "*Colab*" -or $_.MainWindowTitle -like "*Google Chrome*" } | Select-Object -First 1
if (-not $chrome) {
    $chrome = Get-Process -Name chrome, msedge, brave -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
}
if (-not $chrome) {
    Write-Error "Browser with open Google Colab tab not found."
    exit 1
}

# Bring window to foreground
[Win32]::ShowWindow($chrome.MainWindowHandle, 9) # SW_RESTORE
[Win32]::BringWindowToTop($chrome.MainWindowHandle)
[Win32]::SetForegroundWindow($chrome.MainWindowHandle)
Start-Sleep -Milliseconds 400

$root = [System.Windows.Automation.AutomationElement]::FromHandle($chrome.MainWindowHandle)

# Ensure CloudStream_Bridge tab is selected
$tabCond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::TabItem)
$tabs = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $tabCond)
foreach ($tab in $tabs) {
    if ($tab.Current.Name -like "*CloudStream_Bridge*") {
        Write-Output "[*] Switching to Colab tab: $($tab.Current.Name)"
        $selPattern = $tab.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern)
        if ($selPattern) {
            $selPattern.Select()
            Start-Sleep -Milliseconds 600
        }
        break
    }
}

$docCond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::Document)
$allDocs = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $docCond)
$doc = $null
foreach ($d in $allDocs) {
    if ($d.Current.Name -like "*Colab*") {
        $doc = $d
        break
    }
}

if (-not $doc) {
    Write-Error "Colab Document element not found"
    exit 1
}

# Load the code to clipboard
$cellCode = [System.IO.File]::ReadAllText("C:\Users\sahil\colab_cell_code.py")
[System.Windows.Forms.Clipboard]::SetText($cellCode)
Write-Output "[*] Copied $(($cellCode.Length)) characters to clipboard."

# Find Monaco editor
$editCond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty, "Editor content")
$editor = $doc.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $editCond)

if (-not $editor) {
    Write-Error "Editor content control not found"
    exit 1
}

Write-Output "[*] Focusing editor..."
$editor.SetFocus()
Start-Sleep -Milliseconds 300

Write-Output "[*] Pasting autonomous CloudStream runner into cell..."
[System.Windows.Forms.SendKeys]::SendWait("^a")
Start-Sleep -Milliseconds 150
[System.Windows.Forms.SendKeys]::SendWait("^v")
Start-Sleep -Milliseconds 500

# Find and trigger Run button
$btnCond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::AutomationIdProperty, "run-button")
$runBtn = $doc.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $btnCond)

if (-not $runBtn) {
    Write-Error "Run button not found"
    exit 1
}

Write-Output "[*] Triggering cell execution in Google Colab..."
$pattern = $runBtn.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
$pattern.Invoke()
Write-Output "[*] Execution started! Waiting for Cloudflare Tunnel URL..."

# Poll document text for Cloudflare URL
$txtCond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::Text)

$foundUrl = $null
$startTime = [System.DateTime]::Now
$timeout = 90 # 90 seconds timeout

while (([System.DateTime]::Now - $startTime).TotalSeconds -lt $timeout) {
    Start-Sleep -Seconds 3
    $elapsed = [Math]::Round(([System.DateTime]::Now - $startTime).TotalSeconds, 1)

    $texts = $doc.FindAll([System.Windows.Automation.TreeScope]::Descendants, $txtCond)
    foreach ($t in $texts) {
        $name = $t.Current.Name
        if ($name -match 'https://[a-zA-Z0-9-]+\.trycloudflare\.com') {
            $foundUrl = $matches[0]
            Write-Output "`n[+] SUCCESS! Detected Cloudflare Tunnel URL: $foundUrl"
            break
        }
    }

    if ($foundUrl) { break }
    Write-Host -NoNewline "."
}

if ($foundUrl) {
    $hostName = $foundUrl.Replace("https://", "").Replace("http://", "").Trim("/")
    Write-Output ""
    Write-Output "======================================================================"
    Write-Output "🎬 CLOUDSTREAM WEBDAV BRIDGE IS LIVE ON GOOGLE CLOUD!"
    Write-Output "======================================================================"
    Write-Output "🌐 Web UI:       $foundUrl"
    Write-Output "📁 WebDAV URL:   $foundUrl/dav/"
    Write-Output ""
    Write-Output "📱 CX FILE EXPLORER CONFIGURATION:"
    Write-Output "   Protocol:     WebDAV"
    Write-Output "   Host / Server: $hostName"
    Write-Output "   Path:          /dav"
    Write-Output "   Port:          443"
    Write-Output "   Encryption:    CHECKED (HTTPS ON)"
    Write-Output "   Username:      admin"
    Write-Output "   Password:      (leave blank)"
    Write-Output "======================================================================"
} else {
    Write-Output "`n[!] Checking current cell output status..."
    $texts = $doc.FindAll([System.Windows.Automation.TreeScope]::Descendants, $txtCond)
    foreach ($t in $texts) {
        if ($t.Current.Name -like "*Installing*" -or $t.Current.Name -like "*Writing*" -or $t.Current.Name -like "*Starting*" -or $t.Current.Name -like "*Waiting*" -or $t.Current.Name -like "*error*") {
            Write-Output "Output: $($t.Current.Name)"
        }
    }
}
