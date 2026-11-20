"""Listening: VAD, Transcribe streaming, language ID, turn and barge-in
detection. Owner: builder 'listen'. Pure cores (vad.VadSegmenter,
turn.TurnDetector, barge_in.BargeInDetector, language_id.LanguageTracker)
take `now_ms` and return events; pipeline.Listener is the async shell."""
