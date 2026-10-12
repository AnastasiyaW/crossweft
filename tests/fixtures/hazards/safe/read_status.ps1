foreach ($line in Get-Content status.txt) {
    $parts = $line -split '=', 2
    if ($parts.Count -ne 2 -or $parts[1] -notmatch '^[0-9]+$') { exit 2 }  # no verdict
    $code = [int]$parts[1]
    if ($code -ne 0) { exit 1 }
}
