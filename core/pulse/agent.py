from core.pulse.llm import rhodey_tools
from core.llm.fallback import generate_content_with_fallback
from core.llm.config import WorkloadProfile

class HitlInterrupt(Exception):
    def __init__(self, action: str, reason: str, context_data: dict = None):
        self.action = action
        self.reason = reason
        self.context_data = context_data

def ask_user_approval(action: str, reason: str):
    """
    Ask the user for approval before performing a sensitive action. 
    Execution pauses here.
    """
    raise HitlInterrupt(action, reason)

rhodey_tools.register(ask_user_approval)

async def run_agent_loop(prompt: str, model: str, config: dict, max_steps: int = 5):
    """Runs a multi-step agent loop, handling tool calls manually."""
    # Ensure auto-exec is disabled
    if "automatic_function_calling" not in config:
        config["automatic_function_calling"] = {"disable": True}
        
    messages = [{"role": "user", "parts": [{"text": prompt}]}]
    
    for step in range(max_steps):
        response = await generate_content_with_fallback(
            prompt=None,
            workload=WorkloadProfile.SYNTHESIS,
            contents=messages,
            primary_model=model,
            config=config,
            require_json=False
        )
        
        # Append model response to history
        if response.raw_response and hasattr(response.raw_response, 'candidates') and response.raw_response.candidates and response.raw_response.candidates[0].content:
            messages.append({"role": "model", "parts": response.raw_response.candidates[0].content.parts})
            
        if response.function_calls:
            responses_parts = []
            for call in response.function_calls:
                try:
                    result = await rhodey_tools.execute_tool_call(call)
                    responses_parts.append({
                        "function_response": {
                            "name": call.name,
                            "response": {"result": result}
                        }
                    })
                except HitlInterrupt as e:
                    # Bubble up the interrupt to the caller
                    raise e
                except Exception as e:
                    responses_parts.append({
                        "function_response": {
                            "name": call.name,
                            "response": {"error": str(e)}
                        }
                    })
            
            messages.append({"role": "user", "parts": responses_parts})
        else:
            # Done, no more function calls
            return response.text
            
    return response.text
