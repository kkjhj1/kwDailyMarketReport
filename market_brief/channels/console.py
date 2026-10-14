from market_brief.core import Briefing, format_briefing

class ConsoleChannel:
    def send(self, recipient: str, briefing: Briefing) -> str:
        print(f"TO: {recipient}\n{format_briefing(briefing)}")
        return "mock-sent"
