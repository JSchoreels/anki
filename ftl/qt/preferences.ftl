## Video drivers/hardware acceleration. Please avoid translating 'OpenGL' and 'ANGLE'.

preferences-video-driver = Video driver
preferences-video-driver-opengl-mac = OpenGL (recommended on Macs)
preferences-video-driver-software-mac = Software (not recommended)
preferences-video-driver-opengl-other = OpenGL (faster, may cause issues)
preferences-video-driver-software-other = Software (slower)
preferences-video-driver-angle = ANGLE (may work better than OpenGL)
preferences-video-driver-default = default

## RWKV model and recurrent state precision. Keep f32, int8 and int4 unchanged.

preferences-rwkv-state = RWKV
preferences-rwkv-precision = Storage precision
preferences-rwkv-model = Prediction model
preferences-rwkv-model-legacy = Legacy — original model
preferences-rwkv-model-sm40 = SM40 — smaller model
preferences-rwkv-model-sm36 = SM36 Muon — default
preferences-rwkv-model-explanation = Applies to this profile independently of storage precision. Reopen the profile after changing the model; RWKV state is rebuilt from review history. SM36 Muon is the default; explicitly saved model selections are preserved.
preferences-rwkv-f32 = f32 — full precision fallback
preferences-rwkv-int8 = int8 — cards and notes
preferences-rwkv-int4 = int4 — experimental, reduced accuracy
preferences-rwkv-int4-cards-int8-notes = int4 cards + int8 notes — default
preferences-rwkv-precision-explanation = Applies to card and note state in memory and on disk for this profile. Computation remains at full precision. Restart Anki after changing this setting; the next RWKV use rebuilds state from review history. Both int4 options save more memory but can change predictions; int4 cards + int8 notes keeps note state at int8.
