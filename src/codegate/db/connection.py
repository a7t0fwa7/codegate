import asyncio
import copy
import datetime
import json
import uuid
from pathlib import Path
from typing import AsyncGenerator, AsyncIterator, List, Optional

import structlog
from litellm import ChatCompletionRequest, ModelResponse
from pydantic import BaseModel
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine

from codegate.db.models import Alert, Output, Prompt
from codegate.db.queries import (
    AsyncQuerier,
    GetAlertsWithPromptAndOutputRow,
    GetPromptWithOutputsRow,
)

logger = structlog.get_logger("codegate")


class DbCodeGate:

    def __init__(self, sqlite_path: Optional[str] = None):
        # Initialize SQLite database engine with proper async URL
        if not sqlite_path:
            current_dir = Path(__file__).parent
            self._db_path = (current_dir.parent.parent.parent / "codegate.db").absolute()
        else:
            self._db_path = Path(sqlite_path).absolute()

        # Initialize SQLite database engine with proper async URL
        current_dir = Path(__file__).parent
        self._db_path = (current_dir.parent.parent.parent / "codegate.db").absolute()
        logger.debug(f"Initializing DB from path: {self._db_path}")
        engine_dict = {
            "url": f"sqlite+aiosqlite:///{self._db_path}",
            "echo": False,  # Set to False in production
            "isolation_level": "AUTOCOMMIT",  # Required for SQLite
        }
        self._async_db_engine = create_async_engine(**engine_dict)
        self._db_engine = create_engine(**engine_dict)

    def does_db_exist(self):
        return self._db_path.is_file()


class DbRecorder(DbCodeGate):

    def __init__(self, sqlite_path: Optional[str] = None):
        super().__init__(sqlite_path)

        if not self.does_db_exist():
            logger.info(f"Database does not exist at {self._db_path}. Creating..")
            asyncio.run(self.init_db())

    async def init_db(self):
        """Initialize the database with the schema."""
        if self.does_db_exist():
            logger.info("Database already exists. Skipping initialization.")
            return

        # Get the absolute path to the schema file
        current_dir = Path(__file__).parent
        schema_path = current_dir.parent.parent.parent / "sql" / "schema" / "schema.sql"

        if not schema_path.exists():
            raise FileNotFoundError(f"Schema file not found at {schema_path}")

        # Read the schema
        with open(schema_path, "r") as f:
            schema = f.read()

        try:
            # Execute the schema
            async with self._async_db_engine.begin() as conn:
                # Split the schema into individual statements and execute each one
                statements = [stmt.strip() for stmt in schema.split(";") if stmt.strip()]
                for statement in statements:
                    # Use SQLAlchemy text() to create executable SQL statements
                    await conn.execute(text(statement))
        finally:
            await self._async_db_engine.dispose()

    async def _insert_pydantic_model(
        self, model: BaseModel, sql_insert: text
    ) -> Optional[BaseModel]:
        # There are create method in queries.py automatically generated by sqlc
        # However, the methods are buggy for Pydancti and don't work as expected.
        # Manually writing the SQL query to insert Pydantic models.
        async with self._async_db_engine.begin() as conn:
            try:
                result = await conn.execute(sql_insert, model.model_dump())
                row = result.first()
                if row is None:
                    return None

                # Get the class of the Pydantic object to create a new object
                model_class = model.__class__
                return model_class(**row._asdict())
            except Exception as e:
                logger.error(f"Failed to insert model: {model}.", error=str(e))
                return None

    async def record_request(
        self, normalized_request: ChatCompletionRequest, is_fim_request: bool, provider_str: str
    ) -> Optional[Prompt]:
        request_str = None
        if isinstance(normalized_request, BaseModel):
            request_str = normalized_request.model_dump_json(exclude_none=True, exclude_unset=True)
        else:
            try:
                request_str = json.dumps(normalized_request)
            except Exception as e:
                logger.error(f"Failed to serialize output: {normalized_request}", error=str(e))

        if request_str is None:
            logger.warning("No request found to record.")
            return

        # Create a new prompt record
        prompt_params = Prompt(
            id=str(uuid.uuid4()),  # Generate a new UUID for the prompt
            timestamp=datetime.datetime.now(datetime.timezone.utc),
            provider=provider_str,
            type="fim" if is_fim_request else "chat",
            request=request_str,
        )
        sql = text(
            """
                INSERT INTO prompts (id, timestamp, provider, request, type)
                VALUES (:id, :timestamp, :provider, :request, :type)
                RETURNING *
                """
        )
        return await self._insert_pydantic_model(prompt_params, sql)

    async def _record_output(self, prompt: Prompt, output_str: str) -> Optional[Output]:
        output_params = Output(
            id=str(uuid.uuid4()),
            prompt_id=prompt.id,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
            output=output_str,
        )
        sql = text(
            """
                INSERT INTO outputs (id, prompt_id, timestamp, output)
                VALUES (:id, :prompt_id, :timestamp, :output)
                RETURNING *
                """
        )
        return await self._insert_pydantic_model(output_params, sql)

    async def record_output_stream(
        self, prompt: Prompt, model_response: AsyncIterator
    ) -> AsyncGenerator:
        output_chunks = []
        async for chunk in model_response:
            if isinstance(chunk, BaseModel):
                chunk_to_record = chunk.model_dump(exclude_none=True, exclude_unset=True)
                output_chunks.append(chunk_to_record)
            elif isinstance(chunk, dict):
                output_chunks.append(copy.deepcopy(chunk))
            else:
                output_chunks.append({"chunk": str(chunk)})
            yield chunk

        if output_chunks:
            # Record the output chunks
            output_str = json.dumps(output_chunks)
            await self._record_output(prompt, output_str)

    async def record_output_non_stream(
        self, prompt: Optional[Prompt], model_response: ModelResponse
    ) -> Optional[Output]:
        if prompt is None:
            logger.warning("No prompt found to record output.")
            return

        output_str = None
        if isinstance(model_response, BaseModel):
            output_str = model_response.model_dump_json(exclude_none=True, exclude_unset=True)
        else:
            try:
                output_str = json.dumps(model_response)
            except Exception as e:
                logger.error(f"Failed to serialize output: {model_response}", error=str(e))

        if output_str is None:
            logger.warning("No output found to record.")
            return

        return await self._record_output(prompt, output_str)

    async def record_alerts(self, alerts: List[Alert]) -> None:
        if not alerts:
            return
        sql = text(
            """
                INSERT INTO alerts (
                id, prompt_id, code_snippet, trigger_string, trigger_type, trigger_category,
                timestamp
                )
                VALUES (:id, :prompt_id, :code_snippet, :trigger_string, :trigger_type,
                :trigger_category, :timestamp)
                RETURNING *
                """
        )
        # We can insert each alert independently in parallel.
        async with asyncio.TaskGroup() as tg:
            for alert in alerts:
                try:
                    tg.create_task(self._insert_pydantic_model(alert, sql))
                except Exception as e:
                    logger.error(f"Failed to record alert: {alert}.", error=str(e))
        return None


class DbReader(DbCodeGate):

    def __init__(self, sqlite_path: Optional[str] = None):
        super().__init__(sqlite_path)

    async def get_prompts_with_output(self) -> List[GetPromptWithOutputsRow]:
        conn = await self._async_db_engine.connect()
        querier = AsyncQuerier(conn)
        prompts = [prompt async for prompt in querier.get_prompt_with_outputs()]
        await conn.close()
        return prompts

    async def get_alerts_with_prompt_and_output(self) -> List[GetAlertsWithPromptAndOutputRow]:
        conn = await self._async_db_engine.connect()
        querier = AsyncQuerier(conn)
        prompts = [prompt async for prompt in querier.get_alerts_with_prompt_and_output()]
        await conn.close()
        return prompts


def init_db_sync():
    """DB will be initialized in the constructor in case it doesn't exist."""
    db = DbRecorder()
    # Remove the DB file if exists for the moment to not cause issues at schema change.
    # We can replace this in the future with migrations or something similar.
    if db.does_db_exist():
        db._db_path.unlink()
    asyncio.run(db.init_db())


if __name__ == "__main__":
    init_db_sync()
