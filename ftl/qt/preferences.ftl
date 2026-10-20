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
preferences-rwkv-model-explanation = Applies to this profile independently of storage precision. Reopen the profile after changing the model; RWKV state is rebuilt from review history. Existing profiles keep Legacy, and new profiles start with SM40.
preferences-rwkv-f32 = f32 — full precision fallback
preferences-rwkv-int8 = int8 — recommended
preferences-rwkv-int4 = int4 — experimental, reduced accuracy
preferences-rwkv-precision-explanation = Applies to card and note state in memory and on disk for this profile. Computation remains at full precision. Restart Anki after changing this setting; the next RWKV use rebuilds state from review history. int4 saves more memory but can noticeably change predictions.
