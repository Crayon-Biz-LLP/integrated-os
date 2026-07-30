import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import '../theme/app_theme.dart';
import '../services/api_service.dart';
import '../widgets/voice_states.dart';
import 'package:speech_to_text/speech_to_text.dart' as stt;

class QuickCaptureOverlay extends StatefulWidget {
  const QuickCaptureOverlay({super.key});

  @override
  State<QuickCaptureOverlay> createState() => _QuickCaptureOverlayState();
}

class _QuickCaptureOverlayState extends State<QuickCaptureOverlay> {
  final _api = ApiService();
  final _speech = stt.SpeechToText();
  
  VoiceState _voiceState = VoiceState.idle;
  String? _voiceError;
  String _partialText = '';
  
  @override
  void initState() {
    super.initState();
    _startListening();
  }

  Future<void> _startListening() async {
    setState(() => _voiceState = VoiceState.idle);
    
    final available = await _speech.initialize(
      onStatus: (status) {
        if (status == 'done' || status == 'notListening') {
          if (_voiceState == VoiceState.listening) {
            setState(() => _voiceState = VoiceState.idle);
          }
        }
      },
      onError: (error) {
        if (mounted) {
          setState(() {
            _voiceState = VoiceState.error;
            _voiceError = 'Could not hear anything.';
          });
          Future.delayed(const Duration(seconds: 2), () {
            if (mounted) SystemNavigator.pop();
          });
        }
      },
    );

    if (available) {
      setState(() => _voiceState = VoiceState.listening);
      await _speech.listen(
        onResult: (result) {
          if (mounted) {
            setState(() => _partialText = result.recognizedWords);
            if (result.finalResult) {
              _submitCapture(result.recognizedWords);
            }
          }
        },
        listenOptions: stt.SpeechListenOptions(
          listenFor: const Duration(seconds: 15),
          pauseFor: const Duration(seconds: 3),
          cancelOnError: true,
        ),
      );
    } else {
      setState(() {
        _voiceState = VoiceState.error;
        _voiceError = 'Speech recognition unavailable.';
      });
      Future.delayed(const Duration(seconds: 2), () {
        if (mounted) SystemNavigator.pop();
      });
    }
  }

  Future<void> _submitCapture(String text) async {
    if (text.trim().isEmpty) {
      SystemNavigator.pop();
      return;
    }
    
    setState(() => _voiceState = VoiceState.transcribing);
    
    try {
      await _api.sendMessage(text);
      if (mounted) {
        setState(() => _voiceState = VoiceState.confirm);
        Future.delayed(const Duration(seconds: 1), () {
          if (mounted) SystemNavigator.pop();
        });
      }
    } catch (e) {
      if (mounted) {
        setState(() {
          _voiceState = VoiceState.error;
          _voiceError = 'Failed to save.';
        });
        Future.delayed(const Duration(seconds: 2), () {
          if (mounted) SystemNavigator.pop();
        });
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: Colors.black54, // Dim background
      body: SafeArea(
        child: Column(
          mainAxisAlignment: MainAxisAlignment.end,
          children: [
            GestureDetector(
              onTap: () => SystemNavigator.pop(),
              child: Container(
                width: double.infinity,
                padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 32),
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    if (_partialText.isNotEmpty && _voiceState == VoiceState.listening)
                      Padding(
                        padding: const EdgeInsets.only(bottom: 24),
                        child: Text(
                          _partialText,
                          style: const TextStyle(
                            color: AppTheme.textPrimary,
                            fontSize: 24,
                            fontFamily: 'InstrumentSerif',
                            fontStyle: FontStyle.italic,
                          ),
                          textAlign: TextAlign.center,
                        ),
                      ),
                    VoiceStateMachine(
                      state: _voiceState,
                      errorMessage: _voiceError,
                    ),
                    const SizedBox(height: 16),
                    Text(
                      _voiceState == VoiceState.listening ? 'Listening...' :
                      _voiceState == VoiceState.transcribing ? 'Processing...' :
                      _voiceState == VoiceState.confirm ? 'Got it.' :
                      'Tap to dismiss',
                      style: const TextStyle(
                        color: AppTheme.textSecondary,
                        fontSize: 14,
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}