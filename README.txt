CMF Smart Dial Controller - startup visibility fix

1. Extract this folder.
2. Open PowerShell in this folder.
3. Install dependencies:
   py -3.10 -m pip install -r requirements.txt
4. Run:
   py -3.10 cmf_controller.py

The radial menu now opens automatically after startup.
Press F9 to toggle it any time.
Next track / previous track move through menu options.
Play/pause selects the highlighted option (double play/pause toggles menu).

If no window appears, copy the complete PowerShell traceback/output and send it.
For brightness control, Windows laptop display support is required.
Physical dial rotation was not detected in your earlier test; this build does not claim it is solved.
