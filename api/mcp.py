"""MCP information and capability endpoints."""

import json
import logging
import os

from starlette.requests import Request
from starlette.responses import JSONResponse

from .. import mcp_help
from ..engine import build_provider_config
from ..engine.directives import TASK_CLASS_NAMES, list_skill_profiles as _list_skill_profiles

log = logging.getLogger(__name__)


def register(mcp):
    """Register MCP info and capability routes."""
    
    @mcp.custom_route("/capabilities", methods=["GET"])
    async def get_capabilities(request: Request) -> JSONResponse:
        """List available reasoning capabilities and configurations.
        
        Response includes:
        - passes: [2, 3, 4, 5, 6] - available pass counts
        - task_classes: available reasoning modes
        - skills: loaded predefined skill profiles
        - providers: configured providers with available models
        - latency_estimates: estimated latency per pass count and provider
        
        Example response:
        {
            "passes": [2, 3, 4, 5, 6],
            "task_classes": [
                "general", "code_review", "investigation", "safety", "extraction",
                "synthesis", "reasoning", "data_governance", "research_synthesis"
            ],
            "providers": {
                "anthropic": {
                    "available": true,
                    "models": ["claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-7"]
                },
                "ollama": {
                    "available": true,
                    "url": "http://localhost:11434",
                    "models": ["phi4-mini:latest", "qwen3.5:27b", "qwen2.5-coder:7b"]
                }
            },
            "latency_estimates": {
                "2_passes_cloud": "15-30s",
                "3_passes_cloud": "30-60s",
                "2_passes_local": "10-20s"
            }
        }
        """
        try:
            build_provider_config()
            task_classes = list(TASK_CLASS_NAMES)
            skills = _list_skill_profiles()
            
            # Check provider availability
            providers = {}
            
            # Check Anthropic availability
            if os.getenv("ANTHROPIC_API_KEY"):
                providers["anthropic"] = {
                    "available": True,
                    "models": [
                        "claude-haiku-4-5",
                        "claude-sonnet-4-6",
                        "claude-opus-4-7",
                    ]
                }
            else:
                providers["anthropic"] = {"available": False, "models": []}
            providers["copilot"] = {
                "available": bool(os.getenv("GITHUB_COPILOT_OAUTH_TOKEN")),
                "models": ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini"]
                if os.getenv("GITHUB_COPILOT_OAUTH_TOKEN")
                else [],
            }
            
            # Check Ollama availability
            ollama_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
            ollama_available = False
            ollama_models = []
            
            try:
                import requests
                health = requests.get(f"{ollama_url}/api/tags", timeout=2)
                if health.status_code == 200:
                    ollama_available = True
                    data = health.json()
                    ollama_models = [m.get("name", "") for m in data.get("models", [])][:5]
            except Exception:
                pass
            
            providers["ollama"] = {
                "available": ollama_available,
                "url": ollama_url if ollama_available else None,
                "models": ollama_models or ["phi4-mini:latest", "qwen3.5:27b", "qwen2.5-coder:7b"]
            }
            
            # Latency estimates
            latency_estimates = {
                "2_passes_cloud": "15-30s",
                "3_passes_cloud": "30-60s",
                "4_passes_cloud": "60-90s",
                "5_passes_cloud": "90-120s",
                "6_passes_cloud": "120-180s",
                "2_passes_local": "10-20s",
                "3_passes_local": "20-40s",
                "4_passes_local": "40-60s",
                "5_passes_local": "60-80s",
                "6_passes_local": "80-120s",
                "fan_out_3x2": "60-120s (3 perspectives × 2 passes)",
            }
            
            return JSONResponse(
                {
                    "passes": [2, 3, 4, 5, 6],
                    "width_range": [1, 2, 3, 4, 5, 6],
                    "task_classes": task_classes,
                    "skills": skills,
                    "providers": providers,
                    "latency_estimates": latency_estimates,
                },
                status_code=200,
            )
        
        except Exception as e:
            log.exception("Capabilities endpoint error")
            return JSONResponse(
                {"error": f"Failed to get capabilities: {str(e)}"},
                status_code=500,
            )

    @mcp.custom_route("/skills", methods=["GET"])
    async def get_skills(request: Request) -> JSONResponse:
        """List normalized skill profiles loaded from skills/*.yaml."""
        try:
            skills = _list_skill_profiles()
            return JSONResponse({"count": len(skills), "skills": skills}, status_code=200)
        except Exception as e:
            log.exception("Skills endpoint error")
            return JSONResponse(
                {"error": f"Failed to get skills: {str(e)}"},
                status_code=500,
            )

    @mcp.tool()
    async def list_skill_profiles() -> dict:
        """List predefined Deep Think skill profiles loaded from skills/*.yaml."""
        skills = _list_skill_profiles()
        return {"count": len(skills), "skills": skills}

    @mcp.custom_route("/suggest", methods=["POST"])
    async def suggest_reasoning_config(request: Request) -> JSONResponse:
        """Smart request routing based on query complexity.
        
        Request body:
        {
            "query": "user question here",
            "context": "optional context" (optional),
            "prefer_local": false (optional, default false)
        }
        
        Response:
        {
            "recommended_passes": 3,
            "task_class": "general",
            "provider": "cloud",
            "width": 1,
            "height": 1,
            "reasoning": "Query is moderately complex; 3 passes recommended for balanced reasoning time",
            "estimated_latency": "30-60s"
        }
        
        HTTP 200: Suggestion generated
        HTTP 400: Invalid input
        HTTP 500: Internal error
        """
        try:
            body = await request.json()
            query = body.get("query", "").strip()
            context = body.get("context", "").strip()
            prefer_local = body.get("prefer_local", False)
            
            if not query:
                return JSONResponse(
                    {"error": "Missing required field: query"},
                    status_code=400,
                )
            
            # Analyze query complexity
            query_len = len(query)
            complexity = "simple"
            passes = 2
            task_class = "general"
            width = 1
            height = 1
            
            # Detect task class from keywords
            query_lower = query.lower()
            if any(keyword in query_lower for keyword in ["investigate", "evidence", "incident", "threat", "attack", "ioc"]):
                task_class = "investigation"
            elif any(keyword in query_lower for keyword in ["extract", "parse", "schema", "json", "structure", "entity"]):
                task_class = "extraction"
            elif any(keyword in query_lower for keyword in ["write", "summarize", "report", "narrative", "document"]):
                task_class = "synthesis"
            elif any(keyword in query_lower for keyword in ["reason", "logic", "math", "complex", "proof", "algorithm"]):
                task_class = "reasoning"
            elif any(keyword in query_lower for keyword in ["safe", "risk", "policy", "harm", "guardrail", "compliance"]):
                task_class = "safety"
            elif any(keyword in query_lower for keyword in ["code", "bug", "function", "error", "security", "vulnerability"]):
                task_class = "code_review"
            
            # Determine pass count based on complexity
            if query_len < 100:
                passes = 2
                complexity = "simple"
            elif query_len < 300:
                passes = 3
                complexity = "moderate"
            elif query_len < 800:
                passes = 4
                complexity = "complex"
            else:
                passes = 5
                complexity = "very_complex"
            
            # Recommend fan-out for complex investigations
            if task_class in ("investigation", "reasoning") and complexity in ("complex", "very_complex"):
                width = 3
                height = 2
                passes = 1
            
            # Determine provider
            has_api_key = bool(os.getenv("ANTHROPIC_API_KEY"))
            provider = "cloud" if (has_api_key and not prefer_local) else "local"
            
            if not has_api_key:
                provider = "local"
            
            # Estimate latency
            if width > 1:
                estimated_latency = f"{60 * width * height}-{120 * width * height}s (fan-out)"
            else:
                min_lat = 15 * passes
                max_lat = 30 * passes
                estimated_latency = f"{min_lat}-{max_lat}s"
            
            reasoning = f"Query is {complexity}; {passes} passes recommended for {'balanced reasoning time' if passes <= 3 else 'thorough analysis'}."
            if width > 1:
                reasoning += f" Using {width} perspectives with {height} passes each for multi-angle analysis."
            
            return JSONResponse(
                {
                    "recommended_passes": passes if width == 1 else height,
                    "width": width,
                    "height": height,
                    "task_class": task_class,
                    "provider": provider,
                    "complexity": complexity,
                    "reasoning": reasoning,
                    "estimated_latency": estimated_latency,
                },
                status_code=200,
            )
        
        except json.JSONDecodeError:
            return JSONResponse(
                {"error": "Invalid JSON request"},
                status_code=400,
            )
        except Exception as e:
            log.exception("Suggest endpoint error")
            return JSONResponse(
                {"error": f"Failed to generate suggestion: {str(e)}"},
                status_code=500,
            )

    @mcp.custom_route("/mcp/help/{command}", methods=["GET"])
    async def get_help(request: Request) -> JSONResponse:
        """Interactive help for common deep-think commands.
        
        Supported commands:
        - verify: Information about claim verification
        - reason: Information about reasoning passes
        - review: Information about code review
        - escalate: Information about escalation mechanisms
        
        Response:
        {
            "command": "verify",
            "description": "...",
            "usage": "...",
            "example": {...},
            "common_mistakes": [...]
        }
        
        HTTP 200: Help found
        HTTP 404: Unknown command
        HTTP 500: Internal error
        """
        try:
            command = request.path_params.get("command", "").lower()
            
            try:
                help_doc = mcp_help.get_help(command)
                return JSONResponse(help_doc, status_code=200)
            except KeyError:
                return JSONResponse(
                    {
                        "error": f"Unknown command: {command}",
                        "available_commands": mcp_help.get_all_commands()
                    },
                    status_code=404,
                )
        
        except Exception as e:
            log.exception("Help endpoint error")
            return JSONResponse(
                {"error": f"Failed to get help: {str(e)}"},
                status_code=500,
            )
