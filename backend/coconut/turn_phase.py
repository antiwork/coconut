"""The short phases of a turn that the phone can label."""
from __future__ import annotations

# The phone owns the labels. Keep this list in step with BotPhase in the iOS app. The gateway reports `thinking` and
# `writing` (gateway_bridge.TurnEvents).
PHASES = ('thinking', 'working', 'reading', 'editing', 'command', 'testing', 'searching',
          'browsing', 'memory', 'github', 'helpers', 'writing')
