# Network Latency and Packet Loss Tester
# High-precision network monitoring script for Windows
# Date: July 29, 2025
# Version: 1.0
#
# If needed, run the following command before running this script:
# Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser

param(
    [int]$DurationMinutes = 10,
    [string]$TargetHost = "8.8.8.8"
)

# Configuration Variables
$DEFAULT_TARGET = "8.8.8.8"
$PING_INTERVAL = 1 # seconds
$DESKTOP_PATH = [Environment]::GetFolderPath("Desktop")

# Colors for console output
$ErrorColor = "Red"
$SuccessColor = "Green"
$InfoColor = "Cyan"
$WarningColor = "Yellow"

# Global variables for data storage
$global:pingResults = [System.Collections.ArrayList]@()
$global:testStartTime = $null
$global:testTarget = $null

function Write-ColorOutput {
    param([string]$Message, [string]$Color = "White")
    Write-Host $Message -ForegroundColor $Color
}

function Get-NetworkStatistics {
    param(
        [array]$PingResults
    )
    
    $successfulPings = $PingResults | Where-Object { $_.Status -eq "Success" }
    $failedPings = $PingResults | Where-Object { $_.Status -ne "Success" }
    
    if ($successfulPings.Count -eq 0) {
        return @{
            TotalPings = $PingResults.Count
            SuccessfulPings = 0
            FailedPings = $failedPings.Count
            PacketLossPercent = 100
            MinLatency = $null
            MaxLatency = $null
            AvgLatency = $null
            Jitter = $null
        }
    }
    
    $latencies = $successfulPings | ForEach-Object { $_.ResponseTime }
    $minLatency = ($latencies | Measure-Object -Minimum).Minimum
    $maxLatency = ($latencies | Measure-Object -Maximum).Maximum
    $avgLatency = [math]::Round(($latencies | Measure-Object -Average).Average, 2)
    
    # Calculate jitter (standard deviation of latencies)
    $variance = 0
    foreach ($latency in $latencies) {
        $variance += [math]::Pow($latency - $avgLatency, 2)
    }
    $jitter = if ($latencies.Count -gt 1) { 
        [math]::Round([math]::Sqrt($variance / ($latencies.Count - 1)), 2) 
    } else { 0 }
    
    $packetLossPercent = [math]::Round(($failedPings.Count / $PingResults.Count) * 100, 2)
    
    return @{
        TotalPings = $PingResults.Count
        SuccessfulPings = $successfulPings.Count
        FailedPings = $failedPings.Count
        PacketLossPercent = $packetLossPercent
        MinLatency = $minLatency
        MaxLatency = $maxLatency
        AvgLatency = $avgLatency
        Jitter = $jitter
    }
}

function Start-NetworkTest {
    param(
        [string]$Target,
        [int]$Duration
    )
    
    $startTime = Get-Date
    $endTime = $startTime.AddMinutes($Duration)
    $global:testStartTime = $startTime
    $global:testTarget = $Target
    $global:pingResults = [System.Collections.ArrayList]@()
    $pingResults = $global:pingResults
    $pingCount = 0
    
    Write-ColorOutput "===============================================" $InfoColor
    Write-ColorOutput "  Network Latency and Packet Loss Tester" $InfoColor
    Write-ColorOutput "===============================================" $InfoColor
    Write-ColorOutput "Start Time: $($startTime.ToString('yyyy-MM-dd HH:mm:ss'))" $InfoColor
    Write-ColorOutput "Target Host: $Target" $InfoColor
    Write-ColorOutput "Duration: $Duration minutes" $InfoColor
    Write-ColorOutput "Ping Interval: $PING_INTERVAL second(s)" $InfoColor
    Write-ColorOutput "===============================================`n" $InfoColor
    
    # Test initial connectivity
    Write-ColorOutput "Testing initial connectivity..." $InfoColor
    try {
        $initialTest = Test-Connection -ComputerName $Target -Count 1 -ErrorAction Stop
        Write-ColorOutput "Initial connectivity test successful." $SuccessColor
    } catch {
        Write-ColorOutput "Warning: Initial connectivity test failed. Continuing anyway..." $WarningColor
        Write-ColorOutput "Error: $($_.Exception.Message)" $WarningColor
    }
    
    Write-ColorOutput "`nStarting continuous ping test..." $InfoColor
    Write-ColorOutput "Test will run for $Duration minutes. Use Ctrl+C to stop if needed.`n" $WarningColor
    
    while ((Get-Date) -lt $endTime) {
        $pingCount++
        $currentTime = Get-Date
        
        try {
            # Unified approach for all PowerShell versions
            # Test-Connection returns a rich object on success or throws an exception on failure
            $pingResult = Test-Connection -ComputerName $Target -Count 1 -ErrorAction Stop
            $latency = $pingResult.ResponseTime
            $status = "Success"
            
            Write-ColorOutput "[$($currentTime.ToString('HH:mm:ss'))] Ping #$pingCount to $Target : $latency ms" $SuccessColor
            
        } catch {
            $latency = $null
            $status = "Failed"
            
            Write-ColorOutput "[$($currentTime.ToString('HH:mm:ss'))] Ping #$pingCount to $Target : FAILED" $ErrorColor
        }
        
        # Store result using ArrayList Add method for better performance
        $null = $pingResults.Add([PSCustomObject]@{
            Timestamp = $currentTime
            PingNumber = $pingCount
            Target = $Target
            ResponseTime = $latency
            Status = $status
        })
        
        # Simple sleep for the interval
        $sleepTime = $PING_INTERVAL - ((Get-Date) - $currentTime).TotalSeconds
        if ($sleepTime -gt 0) {
            Start-Sleep -Milliseconds ($sleepTime * 1000)
        }
    }
    
    return $pingResults
}

function Generate-Report {
    param(
        [array]$PingResults,
        [string]$Target,
        [datetime]$StartTime,
        [datetime]$EndTime
    )
    
    $stats = Get-NetworkStatistics -PingResults $PingResults
    $duration = ($EndTime - $StartTime).TotalMinutes
    
    $report = @"
===============================================
    NETWORK LATENCY AND PACKET LOSS REPORT
===============================================

Test Configuration:
------------------
Target Host: $Target
Start Time: $($StartTime.ToString('yyyy-MM-dd HH:mm:ss'))
End Time: $($EndTime.ToString('yyyy-MM-dd HH:mm:ss'))
Duration: $([math]::Round($duration, 2)) minutes
Ping Interval: $PING_INTERVAL second(s)

Summary Statistics:
------------------
Total Pings Sent: $($stats.TotalPings)
Successful Pings: $($stats.SuccessfulPings)
Failed Pings: $($stats.FailedPings)
Packet Loss: $($stats.PacketLossPercent)%

Latency Statistics:
------------------
"@

    if ($stats.MinLatency -ne $null) {
        $report += @"
Minimum Latency: $($stats.MinLatency) ms
Maximum Latency: $($stats.MaxLatency) ms
Average Latency: $($stats.AvgLatency) ms
Jitter (Std Dev): $($stats.Jitter) ms
"@
    } else {
        $report += "No successful pings - unable to calculate latency statistics"
    }
    
    $report += @"

Network Quality Assessment:
--------------------------
"@
    
    # Quality assessment
    if ($stats.PacketLossPercent -eq 0) {
        $report += "Packet Loss: EXCELLENT (0% loss)`n"
    } elseif ($stats.PacketLossPercent -lt 1) {
        $report += "Packet Loss: GOOD (<1% loss)`n"
    } elseif ($stats.PacketLossPercent -lt 5) {
        $report += "Packet Loss: FAIR (1-5% loss)`n"
    } else {
        $report += "Packet Loss: POOR (>5% loss)`n"
    }
    
    if ($stats.AvgLatency -ne $null) {
        if ($stats.AvgLatency -lt 20) {
            $report += "Average Latency: EXCELLENT (<20ms)`n"
        } elseif ($stats.AvgLatency -lt 50) {
            $report += "Average Latency: GOOD (20-50ms)`n"
        } elseif ($stats.AvgLatency -lt 100) {
            $report += "Average Latency: FAIR (50-100ms)`n"
        } else {
            $report += "Average Latency: POOR (>100ms)`n"
        }
        
        if ($stats.Jitter -lt 5) {
            $report += "Jitter: EXCELLENT (<5ms)`n"
        } elseif ($stats.Jitter -lt 15) {
            $report += "Jitter: GOOD (5-15ms)`n"
        } elseif ($stats.Jitter -lt 30) {
            $report += "Jitter: FAIR (15-30ms)`n"
        } else {
            $report += "Jitter: POOR (>30ms)`n"
        }
    }
    
    $report += @"

Detailed Results:
----------------
"@
    
    # Add recent ping results (last 10 for brevity in report)
    $recentResults = $PingResults | Select-Object -Last 10
    foreach ($result in $recentResults) {
        $timeStr = $result.Timestamp.ToString('HH:mm:ss')
        if ($result.Status -eq "Success") {
            $report += "[$timeStr] Ping #$($result.PingNumber): $($result.ResponseTime)ms`n"
        } else {
            $report += "[$timeStr] Ping #$($result.PingNumber): FAILED`n"
        }
    }
    
    if ($PingResults.Count -gt 10) {
        $report += "... (showing last 10 results, full log saved to file)`n"
    }
    
    $report += @"

===============================================
Report generated on: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
Script: NetworkLatencyTester.ps1
===============================================
"@
    
    return $report
}

# Main execution
try {
    Write-ColorOutput "Network Latency Tester v1.0" $InfoColor
    Write-ColorOutput "============================`n" $InfoColor
    
    # Check PowerShell version
    Write-ColorOutput "PowerShell Version: $($PSVersionTable.PSVersion)" $InfoColor
    if ($PSVersionTable.PSVersion.Major -lt 5) {
        Write-ColorOutput "Warning: This script requires PowerShell 5.0 or higher." $WarningColor
    }
    
    # Validate parameters
    if ($DurationMinutes -le 0) {
        Write-ColorOutput "Error: Duration must be greater than 0 minutes." $ErrorColor
        exit 1
    }
    
    # Use provided parameters or defaults
    $testTarget = if ($PSBoundParameters.ContainsKey('TargetHost')) { $TargetHost } else { $DEFAULT_TARGET }
    
    Write-ColorOutput "Configuration:" $InfoColor
    Write-ColorOutput "  Target: $testTarget" $InfoColor
    Write-ColorOutput "  Duration: $DurationMinutes minutes`n" $InfoColor
    
    # Start the test
    $testStartTime = Get-Date
    $global:testStartTime = $testStartTime
    $global:testTarget = $testTarget
    $pingResults = Start-NetworkTest -Target $testTarget -Duration $DurationMinutes
    $testEndTime = Get-Date
    
    Write-ColorOutput "`n===============================================" $InfoColor
    Write-ColorOutput "Test completed! Generating report..." $InfoColor
    Write-ColorOutput "===============================================" $InfoColor
    
    # Generate report
    $report = Generate-Report -PingResults $pingResults -Target $testTarget -StartTime $testStartTime -EndTime $testEndTime
    
    # Display report to console
    Write-ColorOutput "`n$report" $SuccessColor
    
    # Save report to desktop with timestamp
    $timestamp = (Get-Date).ToString('yyyy-MM-dd_HH-mm-ss')
    $fileName = "NetworkTest_$timestamp.txt"
    $filePath = Join-Path $DESKTOP_PATH $fileName
    
    try {
        $report | Out-File -FilePath $filePath -Encoding UTF8
        Write-ColorOutput "`nReport saved to: $filePath" $SuccessColor
    } catch {
        Write-ColorOutput "`nWarning: Could not save to desktop. Trying current directory..." $WarningColor
        $filePath = ".\NetworkTest_$timestamp.txt"
        $report | Out-File -FilePath $filePath -Encoding UTF8
        Write-ColorOutput "Report saved to: $filePath" $SuccessColor
    }
    
    # Inform the user and open the report location
    Write-ColorOutput "`nIf you need assistance, please email the report file to your support contact." $InfoColor
    Write-ColorOutput "Opening the report file's location..." $InfoColor
    
    try {
        # This command opens the folder containing the report
        Invoke-Item (Split-Path -Path $filePath -Parent)
    } catch {
        Write-ColorOutput "Could not automatically open the file location. Please find the report at: $filePath" $WarningColor
    }
    
    Write-ColorOutput "`nNetwork latency test completed successfully!" $SuccessColor
    
} catch {
    Write-ColorOutput "An error occurred: $($_.Exception.Message)" $ErrorColor
    Write-ColorOutput "Stack trace: $($_.ScriptStackTrace)" $ErrorColor
    exit 1
}

# Usage examples:
# .\check_latency_win.ps1
# .\check_latency_win.ps1 -DurationMinutes 30
# .\check_latency_win.ps1 -TargetHost "1.1.1.1" -DurationMinutes 15