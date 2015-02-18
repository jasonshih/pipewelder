#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Program entry point"""

from __future__ import print_function

import argparse
import sys
import os
import json
import logging
import contextlib

from urlparse import urlparse
from pipelayer import metadata
from pipelayer.connection import DataPipelineConnection

from awscli.customizations.datapipeline import translator
from boto import connect_s3
from boto.s3.key import Key as S3Key

from pprint import pprint
from copy import deepcopy
from datetime import datetime, timedelta
import re

PIPELINE_DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%S"
PIPELINE_FREQUENCY_RE = re.compile(r'(?P<number>\d+) (?P<unit>\w+s)')
PIPELAYER_STUB_PARAMS = {
    'name': "Pipelayer validation stub",
    'unique_id': 'stub',
    "description": """
This pipeline should always be in 'PENDING' status.
It is used by Pipelayer to validate pipeline definitions.
    """.strip()
}

@contextlib.contextmanager
def cd(new_path):
    saved_path = os.getcwd()
    os.chdir(new_path)
    yield
    os.chdir(saved_path)


def bucket_and_path(s3_uri):
    """
    Return a bucket name and key path from *s3_uri*.

    >>> bucket_and_path('s3://pipelayer-example-bucket/pipelayer-test/inputs')
    ('pipelayer-example-bucket', 'pipelayer-test/inputs')
    """
    uri = urlparse(s3_uri)
    return (uri.netloc, uri.path[1:])


def parse_period(period):
    """
    Return a timedelta object parsed from string *period*.

    >>> parse_period("15 minutes")
    datetime.timedelta(0, 900)
    >>> parse_period("3 hours")
    datetime.timedelta(0, 10800)
    >>> parse_period("1 days")
    datetime.timedelta(1)
    """
    parts = PIPELINE_FREQUENCY_RE.match(period)
    if not parts:
        raise ValueError("'{}' cannot be parsed as a period".format(period))
    parts = parts.groupdict()
    kwargs = {parts['unit']: int(parts['number'])}
    return timedelta(**kwargs)


def adjusted_to_future(timestamp, period):
    """
    Return *timestamp* string, adjusted to the future if necessary.

    If *timestamp* is in the future, it will be returned unchanged.
    If it's in the past, *period* will be repeatedly added until the
    result is in the future.

    All times are assumed to be in UTC.

    >>> adjusted_to_future('2199-01-01T00:00:00', '1 days')
    '2199-01-01T00:00:00'
    """
    dt = datetime.strptime(timestamp, PIPELINE_DATETIME_FORMAT)
    delta = parse_period(period)
    now = datetime.utcnow()
    while dt < now:
        dt += delta
    return dt.strftime(PIPELINE_DATETIME_FORMAT)


def fetch_field_value(aws_response, field_name):
    """
    Return a value nested within the 'fields' of entry of dict *aws_response*.

    The returned value is the second item from a dict with 'key' *field_name*.

    >>> r = {u'fields': [{u'key': u'someKey', u'stringValue': u'someValue'}]}
    >>> fetch_field_value(r, 'someKey')
    u'someValue'
    """
    for container in aws_response['fields']:
        if container['key'] == field_name:
            for (k, v) in container.items():
                if k != 'key':
                    return v
    raise ValueError("Did not find a field called {} in response {}"
                     .format(field_name, response))


def state_from_id(conn, pipeline_id):
    """
    Return the *@pipelineState* string for object matching *pipeline_id*.

    *conn* is a DataPipelineConnection object.
    """
    response = conn.describe_pipelines([pipeline_id])
    description = response['pipelineDescriptionList'][0]
    return fetch_field_value(description, '@pipelineState')


def definition_from_file(filename):
    """
    Return a dict containing the contents of pipeline definition *filename*.
    """
    with open(filename) as f:
        return json.load(f)


def definition_from_id(conn, pipeline_id):
    """
    Return a dict containing the definition of *pipeline_id*.

    *conn* is a DataPipelineConnection object.
    """
    response = conn.get_pipeline_definition(pipeline_id)
    return translator.api_to_definition(response)


class Pipelayer(object):
    """
    A collection of :py:class::`Pipeline`s sharing a definition template.
    """
    def __init__(self, conn, template_path, s3_conn=None):
        """
        Create an empty Pipelayer object.

        *conn* is a DataPipelineConnection used to manipulate added pipelines,
        *s3_conn* is an S3Connection used to upload pipeline tasks to S3,
        and *template_path* is the path to a local file containing the
        template pipeline definition.
        """
        self.conn = conn
        self.s3_conn = s3_conn
        if self.s3_conn is None:
            self.s3_conn = connect_s3()
        template_path = os.path.normpath(template_path)
        self.template = definition_from_file(template_path)
        self.pipelines = {}

    def add_pipeline(self, dirpath):
        """
        Load a new py:class::`Pipeline` object based on the files contained in
        *dirpath*.
        """
        pipeline = Pipeline(self.conn, self.s3_conn, self.template, dirpath)
        self.pipelines[pipeline.name] = pipeline
        return pipeline

    def are_pipelines_valid(self):
        """
        Returns `True` if all pipeline definition validate with AWS.
        """
        return all([p.is_valid() for p in self.pipelines.values()])

    def upload(self):
        """
        Upload files to S3 corresponding to each pipeline and its tasks.
        """
        for p in self.pipelines.values():
            p.upload()

    def activate(self):
        """
        Activate all pipeline definitions,
        deleting existing pipeline if needed.
        """
        if not self.are_pipelines_valid():
            logging.error("Not activating pipelines due to validation errors.")
            return False
        for p in self.pipelines.values():
            p.activate()


class Pipeline(object):
    """
    A class defining a single pipeline definition and associated tasks.
    """
    def __init__(self, conn, s3_conn, template, dirpath):
        """
        Create a Pipeline based on definition dict *template*.

        *dirpath* is a directory containing a 'values.json' file,
        a 'run' executable, and a 'tasks' directory.
        *conn* is a DataPipelineConnection and *s3_conn* is an S3Connection.
        """
        self.conn = conn
        self.s3_conn = s3_conn
        self.dirpath = os.path.normpath(dirpath)
        self.definition = template.copy()
        self.unique_id = 'unique_id'
        values_path = os.path.join(dirpath, 'values.json')
        with open(values_path) as f:
            decoded = json.load(f)
        metadata = decoded.get('metadata', {})
        self.values = decoded.get('values', {})
        self.name = metadata.get('name', os.path.basename(dirpath))
        self.description = metadata.get('description', None)
        timestamp = self.values['myStartDateTime']
        period = self.values['mySchedulePeriod']
        adjusted_timestamp = adjusted_to_future(timestamp, period)
        self.values['myStartDateTime'] = adjusted_timestamp
        pprint(self.values)

    def api_objects(self):
        """
        Return a dict containing the pipeline objects in AWS API format.
        """
        d = deepcopy(self.definition)
        return translator.definition_to_api_objects(d)

    def api_parameters(self):
        """
        Return a dict containing the pipeline parameters in AWS API format.
        """
        d = deepcopy(self.definition)
        return translator.definition_to_api_parameters(d)

    def api_values(self):
        """
        Return a dict containing the pipeline param values in AWS API format.
        """
        d = {'values': self.values}
        return translator.definition_to_parameter_values(d)

    def create(self):
        """
        Create a pipeline in AWS if it does not already exist.

        Returns the pipeline id.
        """
        response = self.conn.create_pipeline(self.name, self.unique_id,
                                             self.description)
        return response['pipelineId']

    def _log_validation_messages(self, response):
        for container in response['validationWarnings']:
            logging.warning("Warnings in validation response for %s",
                            container['id'])
            for message in container['warnings']:
                logging.warning(message)
        for container in response['validationErrors']:
            logging.error("Errors in validation response for %s",
                          container['id'])
            for message in container['errors']:
                logging.error(message)

    def is_valid(self):
        """
        Returns `True` if the pipeline definition validates to AWS.
        """
        response = self.conn.create_pipeline(**PIPELAYER_STUB_PARAMS)
        pipeline_id = response["pipelineId"]
        response = self.conn.validate_pipeline_definition(
            self.api_objects(), pipeline_id,
            self.api_parameters(), self.api_values())
        self._log_validation_messages(response)
        return not response['errored']

    def upload(self):
        """
        Uploads the contents of `dirpath` to S3.

        The destination path in S3 is determined by 'myS3InputDirectory'
        in the 'values.json' file for this pipeline.
        """
        bucket_path, input_dir = bucket_and_path(self.values['myS3InputDir'])
        bucket = self.s3_conn.get_bucket(bucket_path)
        with cd(self.dirpath):
            for root, dirs, files in os.walk('.'):
                for f in files:
                    k = S3Key(bucket)
                    k.key = os.path.join(input_dir, root, f)
                    k.set_contents_from_filename(os.path.join(root, f))

    def activate(self):
        """
        Activate this pipeline definition in AWS.

        Deletes the existing pipeline if it has previously been activated.
        """
        pipeline_id = self.create()
        existing_definition = definition_from_id(self.conn, pipeline_id)
        state = state_from_id(self.conn, pipeline_id)
        if existing_definition == self.definition:
            return True
        elif state != 'PENDING':
            print("State is: ", state)
            logging.info("Deleting pipeline with id {}".format(pipeline_id))
            self.conn.delete_pipeline(pipeline_id)
            return self.activate()
        logging.debug("Putting pipeline definition")
        self.conn.put_pipeline_definition(self.api_objects(),
                                          pipeline_id,
                                          self.api_parameters(),
                                          self.api_values())
        logging.info("Activating pipeline with id {}".format(pipeline_id))
        self.conn.activate_pipeline(pipeline_id)

# def main(argv):
#     """Program entry point.

#     :param argv: command-line arguments
#     :type argv: :class:`list`
#     """
#     author_strings = []
#     for name, email in zip(metadata.authors, metadata.emails):
#         author_strings.append('Author: {0} <{1}>'.format(name, email))

#     epilog = '''
# {project} {version}

# {authors}
# URL: <{url}>
# '''.format(
#         project=metadata.project,
#         version=metadata.version,
#         authors='\n'.join(author_strings),
#         url=metadata.url)

#     arg_parser = argparse.ArgumentParser(
#         prog=argv[0],
#         formatter_class=argparse.RawDescriptionHelpFormatter,
#         description=metadata.description,
#         epilog=epilog)
#     arg_parser.add_argument(
#         '-V', '--version',
#         action='version',
#         version='{0} {1}'.format(metadata.project, metadata.version))

#     arg_parser.parse_args(args=argv[1:])

#     print(epilog

#     return) 0


# def entry_point():
#     """Zero-argument entry point for use with setuptools/distribute."""
#     raise SystemExit(main(sys.argv))


# if __name__ == '__main__':
#     entry_point()
