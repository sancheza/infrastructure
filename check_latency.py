#!/usr/bin/env python3

"""
Script Name: check_latency.py
Description:
    High-precision network latency and packet loss tester for macOS/Linux.
    It wraps the system `ping` command to send a stream of ICMP packets
    at a specified interval, parsing the output to provide detailed statistics
    including min/avg/max/stddev RTT and precise packet loss percentages.

Usage:
    python3 check_latency.py [-h] [-d DURATION] [-t TARGET] [-i INTERVAL]

Examples:
    python3 check_latency.py                      # Default: 10 mins, target 8.8.8.8
    python3 check_latency.py -d 5 -t 1.1.1.1      # 5 mins, target 1.1.1.1
    python3 check_latency.py -i 0.5               # Interval of 0.5s between pings
"""

import subprocess
import re
import argparse
import time
import sys

def run_ping(host, count, interval):
    """
    Runs the ping command and captures its output.
    Args:
        host (str): The target host to ping (e.g., "8.8.8.8").
        count (int): The number of ICMP ECHO_REQUEST packets to send.
        interval (float): The time in seconds to wait between sending packets.
    Returns:
        tuple: A tuple containing (stdout, stderr) from the ping command.
    """
    try:
        # Construct the ping command for macOS
        # -c: count of packets
        # -i: interval between packets
        command = ['ping', '-c', str(count), '-i', str(interval), host]
        
        # Execute the command, capture stdout and stderr
        process = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=True, # Raise an exception for non-zero exit codes
            encoding='utf-8' # Ensure proper encoding for output
        )
        return process.stdout, process.stderr
    except subprocess.CalledProcessError as e:
        print(f"Error running ping command: {e}", file=sys.stderr)
        print(f"STDOUT: {e.stdout}", file=sys.stderr)
        print(f"STDERR: {e.stderr}", file=sys.stderr)
        return None, e.stderr
    except FileNotFoundError:
        print("Error: 'ping' command not found. Please ensure it's in your system's PATH.", file=sys.stderr)
        return None, "ping command not found"
    except Exception as e:
        print(f"An unexpected error occurred: {e}", file=sys.stderr)
        return None, str(e)

def parse_ping_output(output):
    """
    Parses the stdout of the ping command to extract latency and packet loss.
    Args:
        output (str): The standard output string from the ping command.
    Returns:
        dict: A dictionary containing parsed statistics (min_rtt, avg_rtt, max_rtt,
              stddev_rtt, packet_loss_percent, packets_transmitted, packets_received).
              Returns None if parsing fails.
    """
    stats = {
        'min_rtt': None,
        'avg_rtt': None,
        'max_rtt': None,
        'stddev_rtt': None,
        'packet_loss_percent': None,
        'packets_transmitted': None,
        'packets_received': None
    }

    # Regex to find the packet loss line
    packet_loss_match = re.search(
        r'(\d+) packets transmitted, (\d+) packets received, (\d+\.?\d*)% packet loss',
        output
    )
    if packet_loss_match:
        stats['packets_transmitted'] = int(packet_loss_match.group(1))
        stats['packets_received'] = int(packet_loss_match.group(2))
        stats['packet_loss_percent'] = float(packet_loss_match.group(3))

    # Regex to find the round-trip time statistics line
    rtt_match = re.search(
        r'round-trip min/avg/max/stddev = (\d+\.?\d*)/(\d+\.?\d*)/(\d+\.?\d*)/(\d+\.?\d*) ms',
        output
    )
    if rtt_match:
        stats['min_rtt'] = float(rtt_match.group(1))
        stats['avg_rtt'] = float(rtt_match.group(2))
        stats['max_rtt'] = float(rtt_match.group(3))
        stats['stddev_rtt'] = float(rtt_match.group(4))
    
    # Check if essential data was parsed
    if stats['packet_loss_percent'] is not None and stats['avg_rtt'] is not None:
        return stats
    else:
        print("Warning: Could not parse all ping statistics from output.", file=sys.stderr)
        print(f"Ping output:\n{output}", file=sys.stderr)
        return None

def main():
    """
    Main function to parse arguments, run the test, and display results.
    """
    parser = argparse.ArgumentParser(
        description="Measure network latency and packet loss with high precision."
    )
    parser.add_argument(
        '-d', '--duration',
        type=int,
        default=10,
        help="Duration of the test in minutes (default: 10 minutes)."
    )
    parser.add_argument(
        '-t', '--target',
        type=str,
        default="8.8.8.8", # Google DNS server, a common reliable target
        help="Target host IP address or hostname (default: 8.8.8.8)."
    )
    parser.add_argument(
        '-i', '--interval',
        type=float,
        default=0.2, # Send a packet every 0.2 seconds for precision
        help="Interval between ping packets in seconds (default: 0.2s)."
    )

    args = parser.parse_args()

    duration_minutes = args.duration
    target_host = args.target
    ping_interval = args.interval

    if duration_minutes <= 0:
        print("Error: Duration must be a positive number of minutes.", file=sys.stderr)
        sys.exit(1)
    if ping_interval <= 0:
        print("Error: Ping interval must be a positive number of seconds.", file=sys.stderr)
        sys.exit(1)

    duration_seconds = duration_minutes * 60
    
    # Calculate total packets to send based on duration and interval
    # We add a small buffer to ensure we cover the full duration
    total_packets = int(duration_seconds / ping_interval) + 1

    print(f"Starting network latency and packet loss test...")
    print(f"Target Host: {target_host}")
    print(f"Test Duration: {duration_minutes} minutes ({duration_seconds} seconds)")
    print(f"Ping Interval: {ping_interval} seconds")
    print(f"Total Packets to Send: {total_packets}")
    print("-" * 40)

    start_time = time.time()
    
    # Execute the ping command
    stdout, stderr = run_ping(target_host, total_packets, ping_interval)

    end_time = time.time()
    actual_duration = end_time - start_time

    print("-" * 40)
    print("Test Complete. Generating Summary...")
    print("-" * 40)

    if stdout:
        ping_stats = parse_ping_output(stdout)
        if ping_stats:
            print("\n--- Detailed Summary ---")
            print(f"Target Host: {target_host}")
            print(f"Actual Test Duration: {actual_duration:.2f} seconds")
            print(f"Total Packets Transmitted: {ping_stats['packets_transmitted']}")
            print(f"Total Packets Received: {ping_stats['packets_received']}")
            print(f"Total Packet Loss: {ping_stats['packet_loss_percent']:.2f}%")
            print("\n--- Round Trip Time (RTT) Statistics ---")
            print(f"Minimum RTT: {ping_stats['min_rtt']:.3f} ms")
            print(f"Average RTT: {ping_stats['avg_rtt']:.3f} ms")
            print(f"Maximum RTT: {ping_stats['max_rtt']:.3f} ms")
            print(f"Standard Deviation RTT: {ping_stats['stddev_rtt']:.3f} ms")
            print("\nNote: Precision is achieved by sending many small, frequent pings.")
        else:
            print("Failed to parse ping output. Please check the console for errors.", file=sys.stderr)
    else:
        print("Ping command failed to execute or returned no output. Cannot generate summary.", file=sys.stderr)
        if stderr:
            print(f"Error details: {stderr}", file=sys.stderr)

if __name__ == "__main__":
    main()

