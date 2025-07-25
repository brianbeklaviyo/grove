# Copyright (c) HashiCorp, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Google BigQuery connector for Grove."""

import json
from datetime import datetime, timedelta, timezone

from google.auth.exceptions import GoogleAuthError
from google.cloud import bigquery
from google.oauth2 import service_account

from grove.connectors import BaseConnector
from grove.constants import CHRONOLOGICAL
from grove.exceptions import (
    ConfigurationException,
    NotFoundException,
    RequestFailedException,
)


class Connector(BaseConnector):
    CONNECTOR = "google_bigquery_query"
    LOG_ORDER = CHRONOLOGICAL

    def collect(self):
        """Collects logs from the specified table using Google BigQuery API."""
        self.logger.info("Starting data collection from BigQuery.")

        try:
            project_id = self.configuration.project_id
            dataset_name = self.configuration.dataset_name
            table_name = self.configuration.table_name
            columns = self.configuration.columns
            max_batches = getattr(self.configuration, "max_batches", 3)
            self.POINTER_PATH = self.configuration.pointer_path
            time_format = getattr(self.configuration, "time_format", "microseconds")

            self.logger.debug("Configuration parameters:")
            self.logger.debug(f"Project ID: {project_id}")
            self.logger.debug(f"Dataset Name: {dataset_name}")
            self.logger.debug(f"Table Name: {table_name}")
            self.logger.debug(f"Columns: {columns}")

            if not self.POINTER_PATH:
                raise ConfigurationException(
                    "POINTER_PATH is not set in the configuration."
                )

            if not isinstance(max_batches, int) or max_batches <= 0:
                raise ConfigurationException("max_batches must be a positive integer.")

            for value in [project_id, dataset_name, table_name]:
                if not isinstance(value, str):
                    raise ConfigurationException(f"{value} must be a string")

            if not isinstance(columns, list):
                raise ConfigurationException("columns must be a list.")

            if time_format not in ["microseconds", "timestamp"]:
                raise ConfigurationException(
                    "time_format must be either 'microseconds' or 'timestamp'"
                )
        except AttributeError as err:
            raise ConfigurationException(
                f"Missing required configuration attribute: {err}"
            )

        self.logger.info("BigQuery connector configured successfully.")

        try:
            client = bigquery.Client(
                credentials=self.get_credentials(), project=project_id
            )
            self.logger.debug("BigQuery client created successfully.")
        except Exception as e:
            self.logger.error(f"Failed to create BigQuery client: {e}")
            raise

        # Handle pointer retrieval based on time_format
        try:
            stored_pointer = self.pointer
            self.logger.debug(
                f"Stored pointer: {stored_pointer} ({type(stored_pointer)})"
            )

            # Validate pointer format matches time_format (if pointer exists)
            if stored_pointer and stored_pointer.strip():
                if time_format == "microseconds":
                    try:
                        int(stored_pointer)  # Validate it's a valid integer
                    except ValueError:
                        raise ConfigurationException(
                            f"Pointer '{stored_pointer}' is not a valid microseconds value"
                        )
                else:  # timestamp
                    try:
                        datetime.fromisoformat(stored_pointer.replace("+00", "+00:00"))
                    except ValueError:
                        raise ConfigurationException(
                            f"Pointer '{stored_pointer}' is not a valid timestamp format"
                        )

            # Store pointer in native format
            if time_format == "microseconds":
                pointer_epoch_usec = int(stored_pointer)
                self.pointer = str(pointer_epoch_usec)
            else:  # timestamp
                self.pointer = stored_pointer  # Keep original timestamp string

        except (NotFoundException, ValueError, ConfigurationException):
            # Set to a week ago based on time_format
            week_ago = datetime.utcnow() - timedelta(days=7)
            week_ago = week_ago.replace(tzinfo=timezone.utc)

            if time_format == "microseconds":
                pointer_epoch_usec = int(week_ago.timestamp() * 1_000_000)
                self.pointer = str(pointer_epoch_usec)
            else:  # timestamp
                self.pointer = week_ago.strftime("%Y-%m-%d %H:%M:%S+00")
                pointer_epoch_usec = int(week_ago.timestamp() * 1_000_000)

            self.logger.debug(
                f"No valid pointer found. Setting pointer to: {self.pointer}"
            )

        # Configuration for batching
        all_rows = []
        batch_count = 0

        while True:
            if time_format == "microseconds":
                query_pointer = pointer_epoch_usec
                self.logger.debug(
                    f"Pointer for query (microseconds): {query_pointer} ({type(query_pointer)})"
                )
                where_clause = f"{self.POINTER_PATH} > {query_pointer}"
            else:  # timestamp
                # Use the original timestamp string directly
                where_clause = f"{self.POINTER_PATH} > TIMESTAMP('{self.pointer}')"

            query = f"""
            SELECT {', '.join(columns)}
            FROM `{project_id}.{dataset_name}.{table_name}`
            WHERE {where_clause}
            AND {self.POINTER_PATH} IS NOT NULL
            ORDER BY {self.POINTER_PATH} ASC
            LIMIT 1000
            """
            self.logger.debug(f"Constructed query: {query}")

            try:
                self.logger.info("Executing query on BigQuery.")
                query_job = client.query(query)
                results = query_job.result()
                self.logger.debug("Query executed successfully.")

                rows = [dict(row) for row in results]
                if not rows:
                    self.logger.info("No more logs found.")
                    break

                self.logger.info(
                    f"Collected {len(rows)} logs in batch {batch_count + 1}."
                )
                all_rows.extend(rows)
                batch_count += 1

                # Save and break if we've collected enough batches or reached the end
                if batch_count >= max_batches or len(rows) < 1000:
                    if all_rows:
                        self.logger.debug(
                            f"Saving {len(all_rows)} total logs from {batch_count} batches."
                        )
                        self.save(all_rows)
                    break

            except Exception as err:
                self.logger.error(f"BigQuery query failed: {err}")
                raise RequestFailedException(f"BigQuery query failed: {err}")

    def get_credentials(self):
        """Generates and returns a credentials instance from the connector's configured
        service account info. This is used for required to perform operations using the
        Google API client.

        :return: A credentials instance built from configured service account info.

        :raises ConfigurationException: There is an issue with the configuration
            for this connector.
        """
        try:
            service_account_info = json.loads(self.key)
        except json.JSONDecodeError as err:
            raise ConfigurationException(
                f"Unable to load service account JSON for {self.identity}: {err}"
            )

        # Construct the credentials, including scopes and delegation.
        # Subject not needed for Bigquery API
        try:
            credentials = service_account.Credentials.from_service_account_info(
                service_account_info,
                scopes=["https://www.googleapis.com/auth/bigquery"],
            )
        except GoogleAuthError as err:
            raise ConfigurationException(
                f"Authentication error while generating credentials for {self.identity}: {err}"
            )
        except ValueError as err:
            raise ConfigurationException(
                f"Unable to generate credentials from service account info for {self.identity}: {err}"
            )

        return credentials
