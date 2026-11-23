@echo off
rem Project Mu portable launcher: starts Mu with its theme and New Tab page.
rem (Builds with patch 0007 load them by themselves; this also covers older ones.)
start "" "%~dp0chrome.exe" --load-extension="%~dp0mu\theme,%~dp0mu\newtab" %*
