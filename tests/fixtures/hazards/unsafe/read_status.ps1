foreach ($line in Get-Content status.txt) {
    $parts = $line -split '=', 2
    $code = [int]$parts[1]
    if ($code -ne 0) { exit 1 }
}
