; NSIS hooks for the Jarvis installer.
;
; Two jobs, and the second is the one that matters.
;
; On install: nothing beyond what Tauri already does. The model downloads and
; the first-run setup happen inside the app, where there is a progress bar and
; a way to say no, not inside an installer that cannot ask properly.
;
; On uninstall: stop the core before removing files, and ask — once, clearly —
; whether to delete the user's data. An uninstaller that silently deletes a
; year of conversation history and learned preferences is doing something the
; user did not ask for; one that silently leaves it behind is lying about
; having uninstalled. So it asks, and it defaults to keeping.

!macro NSIS_HOOK_PREINSTALL
  DetailPrint "Stopping any running Jarvis core..."
  ; The sidecar is a child of the app, but a crashed app can leave it behind,
  ; and a running executable cannot be overwritten.
  nsExec::Exec 'taskkill /F /IM jarvis-core.exe /T'
  Pop $0
!macroend

!macro NSIS_HOOK_POSTINSTALL
  DetailPrint "Jarvis installed. Voice and semantic memory models are downloaded"
  DetailPrint "from inside the app when you turn those features on."
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  DetailPrint "Stopping Jarvis..."
  nsExec::Exec 'taskkill /F /IM jarvis-core.exe /T'
  Pop $0
!macroend

!macro NSIS_HOOK_POSTUNINSTALL
  ; $LOCALAPPDATA\jarvis holds the database (conversations, memory, audit log),
  ; downloaded models, and installed plugins. Several hundred megabytes, and
  ; none of it reinstallable.
  MessageBox MB_YESNO|MB_ICONQUESTION|MB_DEFBUTTON2 \
    "Also delete your Jarvis data?$\r$\n$\r$\n\
     This removes your conversation history, everything Jarvis learned about \
     you, the audit log, any downloaded models, and installed plugins.$\r$\n$\r$\n\
     Choose No to keep it, in case you reinstall." \
    IDNO keep_data
    DetailPrint "Removing Jarvis data..."
    RMDir /r "$LOCALAPPDATA\jarvis"
    RMDir /r "$APPDATA\jarvis"
    Goto done
  keep_data:
    DetailPrint "Your Jarvis data has been left in $LOCALAPPDATA\jarvis"
  done:
!macroend
