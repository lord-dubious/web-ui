from __future__ import annotations

import asyncio
import logging
import os
import random
from typing import List

# from lmnr.sdk.decorators import observe
from browser_use.agent.gif import create_history_gif
from browser_use.agent.message_manager.utils import is_model_without_tool_support
from browser_use.agent.service import Agent, AgentHookFunc
from browser_use.agent.views import (
    ActionResult,
    AgentHistory,
    AgentHistoryList,
    AgentStepInfo,
    ToolCallingMethod,
)
from browser_use.browser.views import BrowserStateHistory
from browser_use.controller.registry.views import ActionModel
from browser_use.utils import time_execution_async
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

SKIP_LLM_API_KEY_VERIFICATION = (
    os.environ.get("SKIP_LLM_API_KEY_VERIFICATION", "false").lower()[0] in "ty1"
)


class BrowserUseAgent(Agent):
    def __init__(self, *args, **kwargs):
        """Initialize the agent with cached delay settings for better performance."""
        super().__init__(*args, **kwargs)
        self._delay_settings_cache = {}
        self._cache_delay_settings()

    def _cache_delay_settings(self):
        """Cache delay settings from environment variables to avoid repeated file reads."""
        delay_types = ["STEP", "ACTION", "TASK"]

        for delay_type in delay_types:
            # Cache random interval settings
            enable_random_str = os.environ.get(
                f"{delay_type}_ENABLE_RANDOM_INTERVAL", "false"
            )
            enable_random = enable_random_str.lower() == "true"

            # Cache fixed delay settings
            delay_minutes_str = os.environ.get(f"{delay_type}_DELAY_MINUTES", "0.0")
            if not delay_minutes_str:
                delay_minutes_str = "0.0"

            # Cache random delay range settings
            min_delay_str = os.environ.get(f"{delay_type}_MIN_DELAY_MINUTES", "0.0")
            if not min_delay_str:
                min_delay_str = "0.0"

            max_delay_str = os.environ.get(f"{delay_type}_MAX_DELAY_MINUTES", "0.0")
            if not max_delay_str:
                max_delay_str = "0.0"

            self._delay_settings_cache[delay_type] = {
                "enable_random": enable_random,
                "delay_minutes": delay_minutes_str,
                "min_delay_minutes": min_delay_str,
                "max_delay_minutes": max_delay_str,
            }

        logger.debug(f"Cached delay settings: {self._delay_settings_cache}")

    def invalidate_delay_cache(self):
        """Invalidate and refresh the delay settings cache."""
        self._cache_delay_settings()
        logger.debug("Delay settings cache invalidated and refreshed")

    def _set_tool_calling_method(self) -> ToolCallingMethod | None:
        tool_calling_method = self.settings.tool_calling_method
        if tool_calling_method == "auto":
            if is_model_without_tool_support(self.model_name):
                return "raw"
            elif self.chat_model_library == "ChatGoogleGenerativeAI":
                return None
            elif self.chat_model_library == "ChatOpenAI":
                return "function_calling"
            elif self.chat_model_library == "AzureChatOpenAI":
                return "function_calling"
            else:
                return None
        else:
            return tool_calling_method

    async def multi_act(
        self, actions: List[ActionModel], check_for_new_elements: bool = True
    ) -> List[ActionResult]:
        """
        Override the parent multi_act method to add delays between individual actions.

        Args:
            actions: List of actions to execute
            check_for_new_elements: Whether to check for new elements after each action

        Returns:
            List[ActionResult] from executing all actions
        """
        if not actions:
            return []

        # Execute the first action without delay
        results = await super().multi_act(
            [actions[0]], check_for_new_elements=check_for_new_elements
        )

        # Execute remaining actions with delays between them
        for action in actions[1:]:
            # Apply ACTION delay between individual actions
            await self._apply_delay("ACTION")

            # Execute the next action
            next_results = await super().multi_act(
                [action], check_for_new_elements=check_for_new_elements
            )

            # Combine results
            results.extend(next_results)

        return results

    async def _apply_delay(self, delay_type: str) -> None:
        """
        Apply a delay based on the delay type (STEP, ACTION, or TASK).
        Uses cached settings for better performance.

        Args:
            delay_type: Type of delay to apply ("STEP", "ACTION", or "TASK")
        """
        # Get cached settings for this delay type
        settings = self._delay_settings_cache.get(delay_type)
        if not settings:
            logger.warning(f"No cached settings found for delay type: {delay_type}")
            return

        enable_random_delay = settings["enable_random"]

        if enable_random_delay:
            # Use cached random delay settings
            min_delay_minutes_str = settings["min_delay_minutes"]
            max_delay_minutes_str = settings["max_delay_minutes"]

            try:
                min_delay_minutes_raw = float(min_delay_minutes_str)
                max_delay_minutes_raw = float(max_delay_minutes_str)

                min_seconds_raw = min_delay_minutes_raw * 60
                max_seconds_raw = max_delay_minutes_raw * 60

                actual_min_seconds = min(min_seconds_raw, max_seconds_raw)
                actual_max_seconds = max(min_seconds_raw, max_seconds_raw)

                if actual_max_seconds > 0.0:
                    random_delay_seconds = random.uniform(
                        actual_min_seconds, actual_max_seconds
                    )
                    delay_minutes = random_delay_seconds / 60
                    logger.info(
                        f"Applying random {delay_type.lower()} delay between {actual_min_seconds / 60:.1f} and "
                        f"{actual_max_seconds / 60:.1f} minutes. Chosen: {random_delay_seconds:.1f} seconds "
                        f"({delay_minutes:.2f} minutes)."
                    )
                    await asyncio.sleep(random_delay_seconds)
                else:
                    logger.info(
                        f"Random {delay_type.lower()} delay is enabled but min/max values result in no delay."
                    )

            except ValueError:
                logger.warning(
                    f"Invalid cached values for {delay_type} random delay: "
                    f"min='{min_delay_minutes_str}', max='{max_delay_minutes_str}'. Expected floats."
                )
        else:
            # Use cached fixed delay settings
            delay_minutes_str = settings["delay_minutes"]

            try:
                delay_minutes = float(delay_minutes_str)
                if delay_minutes > 0.0:
                    delay_seconds = delay_minutes * 60
                    logger.info(
                        f"Waiting for fixed {delay_type.lower()} delay of {delay_seconds:.1f} seconds "
                        f"({delay_minutes:.2f} minutes)..."
                    )
                    await asyncio.sleep(delay_seconds)
            except ValueError:
                logger.warning(
                    f"Invalid cached value for {delay_type}_DELAY_MINUTES: '{delay_minutes_str}'. Expected a float."
                )

    @time_execution_async("--run (agent)")
    async def run(
        self,
        max_steps: int = 100,
        on_step_start: AgentHookFunc | None = None,
        on_step_end: AgentHookFunc | None = None,
    ) -> AgentHistoryList:
        """Execute the task with maximum number of steps"""

        loop = asyncio.get_event_loop()

        # Set up the Ctrl+C signal handler with callbacks specific to this agent
        from browser_use.utils import SignalHandler

        signal_handler = SignalHandler(
            loop=loop,
            pause_callback=self.pause,
            resume_callback=self.resume,
            custom_exit_callback=None,  # No special cleanup needed on forced exit
            exit_on_second_int=True,
        )
        signal_handler.register()

        try:
            self._log_agent_run()

            # Execute initial actions if provided
            if self.initial_actions:
                result = await self.multi_act(
                    self.initial_actions, check_for_new_elements=False
                )
                self.state.last_result = result

            for step in range(max_steps):
                # Check if waiting for user input after Ctrl+C
                if self.state.paused:
                    signal_handler.wait_for_resume()
                    signal_handler.reset()

                # Check if we should stop due to too many failures
                if self.state.consecutive_failures >= self.settings.max_failures:
                    logger.error(
                        f"❌ Stopping due to {self.settings.max_failures} consecutive failures"
                    )
                    break

                # Check control flags before each step
                if self.state.stopped:
                    logger.info("Agent stopped")
                    break

                while self.state.paused:
                    await asyncio.sleep(0.2)  # Small delay to prevent CPU spinning
                    if self.state.stopped:  # Allow stopping while paused
                        break

                if on_step_start is not None:
                    await on_step_start(self)

                # Process step delay
                await self._apply_delay("STEP")

                # Process task delay (if applicable for current task/run)
                # Note: Task delay might not be applicable depending on implementation
                if step == 0:  # Only apply task delay on first step
                    await self._apply_delay("TASK")

                step_info = AgentStepInfo(step_number=step, max_steps=max_steps)
                await self.step(step_info)

                if on_step_end is not None:
                    await on_step_end(self)

                if self.state.history.is_done():
                    if self.settings.validate_output and step < max_steps - 1:
                        if not await self._validate_output():
                            continue

                    await self.log_completion()
                    break
            else:
                error_message = "Failed to complete task in maximum steps"

                self.state.history.history.append(
                    AgentHistory(
                        model_output=None,
                        result=[
                            ActionResult(error=error_message, include_in_memory=True)
                        ],
                        state=BrowserStateHistory(
                            url="",
                            title="",
                            tabs=[],
                            interacted_element=[],
                            screenshot=None,
                        ),
                        metadata=None,
                    )
                )

                logger.info(f"❌ {error_message}")

            return self.state.history

        except KeyboardInterrupt:
            # Already handled by our signal handler, but catch any direct KeyboardInterrupt as well
            logger.info(
                "Got KeyboardInterrupt during execution, returning current history"
            )
            return self.state.history

        finally:
            # Unregister signal handlers before cleanup
            signal_handler.unregister()

            if self.settings.save_playwright_script_path:
                logger.info(
                    f"Agent run finished. Attempting to save Playwright script to: {self.settings.save_playwright_script_path}"
                )
                try:
                    # Extract sensitive data keys if sensitive_data is provided
                    keys = (
                        list(self.sensitive_data.keys())
                        if self.sensitive_data
                        else None
                    )
                    # Pass browser and context config to the saving method
                    self.state.history.save_as_playwright_script(
                        self.settings.save_playwright_script_path,
                        sensitive_data_keys=keys,
                        browser_config=self.browser.config,
                        context_config=self.browser_context.config,
                    )
                except Exception as script_gen_err:
                    # Log any error during script generation/saving
                    logger.error(
                        f"Failed to save Playwright script: {script_gen_err}",
                        exc_info=True,
                    )

            await self.close()

            if self.settings.generate_gif:
                output_path: str = "agent_history.gif"
                if isinstance(self.settings.generate_gif, str):
                    output_path = self.settings.generate_gif

                create_history_gif(
                    task=self.task, history=self.state.history, output_path=output_path
                )
