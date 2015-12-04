# Copyright 2015 Google Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Common utilities for Python code generated by Veneer Toolkit.

This will eventually be part of GAX."""

from grpc.beta import implementations
from oauth2client import client as auth_client


def _oauth_access_token(scopes):
  google_creds = auth_client.GoogleCredentials.get_application_default()
  scoped_creds = google_creds.create_scoped(scopes)
  return scoped_creds.get_access_token().access_token


def create_stub(
    generated_create_stub, service_path, port, ssl_creds=None, channel=None,
    metadata_transformer=None, scopes=[]):
  """Creates a gRPC client stub.

  Args:
    service_path: The DNS of the API remote host.
    port: The port on which to connect to the remote host.
    ssl_creds: A ClientCredentials object for use with an SSL-enabled Channel.
      If none, credentials are pulled from a default location.
    channel: A Channel object through which to make calls. If none, a secure
      channel is constructed.
    metadata_transformer: A function that transforms the metadata for
      requests, e.g., to give OAuth credentials.
    scopes: The OAuth scopes for this service. This parameter is ignored if
      a custom metadata_transformer is supplied.
    generated_create_stub: The generated gRPC method to create a stub.

  Returns:
    A gRPC client stub.
  """
  if channel is None:
    if ssl_creds is None:
      ssl_creds = implementations.ssl_client_credentials(None, None, None)
    else:
      ssl_creds = ssl_creds
    channel = implementations.secure_channel(service_path, port, ssl_creds)
  else:
    channel = channel


  if metadata_transformer is None:
    metadata_transformer = lambda x: [
        ('Authorization', 'Bearer %s'% _oauth_access_token(scopes))]
  else:
    metadata_transformer = metadata_transformer

  return generated_create_stub(channel, metadata_transformer=metadata_transformer)

