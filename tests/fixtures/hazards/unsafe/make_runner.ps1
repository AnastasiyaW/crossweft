# writes a batch runner; the batch text lives in this file's strings
$runner = @(
    '@echo off',
    'call step_one.cmd',
    'echo step_one=%ERRORLEVEL%>>"status.txt"'
)
Set-Content -Path runner.cmd -Value $runner
