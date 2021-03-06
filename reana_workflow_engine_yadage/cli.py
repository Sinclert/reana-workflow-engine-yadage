# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2017, 2018 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.
"""REANA-Workflow-Engine-yadage command line interface."""

from __future__ import absolute_import, print_function

import base64
import json
import logging
import os
import yaml

import click
import yadageschemas
from reana_commons.config import (
    REANA_LOG_FORMAT,
    REANA_LOG_LEVEL,
    REANA_WORKFLOW_UMASK,
    SHARED_VOLUME_PATH,
)
from reana_commons.utils import check_connection_to_job_controller
from yadage.steering_api import steering_ctx
from yadage.utils import setupbackend_fromstring

from .config import LOGGING_MODULE
from .tracker import REANATracker
from .utils import REANAWorkflowStatusPublisher

logging.basicConfig(level=REANA_LOG_LEVEL, format=REANA_LOG_FORMAT)
log = logging.getLogger(LOGGING_MODULE)


def load_json(ctx, param, value):
    """Decode and load json for click option."""
    value = value[1:]
    return json.loads(base64.standard_b64decode(value).decode())


def load_yadage_operational_options(ctx, param, operational_options):
    """Decode and prepare operational options."""
    operational_options = load_json(ctx, param, operational_options)
    workflow_workspace = ctx.params.get("workflow_workspace")
    workflow_workspace = "{0}/{1}".format(SHARED_VOLUME_PATH, workflow_workspace)
    toplevel = operational_options.get("toplevel", "")
    if not toplevel.startswith("github:"):
        toplevel = os.path.join(workflow_workspace, toplevel)
    operational_options["toplevel"] = toplevel

    operational_options["initdir"] = os.path.join(
        workflow_workspace, operational_options.get("initdir", "")
    )

    operational_options["initfiles"] = [
        os.path.join(workflow_workspace, initfile)
        for initfile in operational_options.get("initfiles", [])
    ]

    return operational_options


@click.command()
@click.option("--workflow-uuid", required=True, help="UUID of workflow to be run.")
@click.option(
    "--workflow-workspace",
    required=True,
    help="Name of workspace in which workflow should run.",
)
@click.option(
    "--workflow-file",
    required=True,
    help="Path to the workflow file. This field is used when"
    " no workflow JSON has been passed.",
)
@click.option(
    "--workflow-parameters",
    help="JSON representation of workflow_parameters received by" " the workflow.",
    callback=load_json,
)
@click.option(
    "--operational-options",
    help="Options to be passed to the workflow engine" " (e.g. initdir).",
    callback=load_yadage_operational_options,
)
def run_yadage_workflow(
    workflow_uuid,
    workflow_workspace,
    workflow_file,
    workflow_parameters=None,
    operational_options={},
):
    """Run a ``yadage`` workflow."""
    log.info("getting socket..")
    workflow_workspace = "{0}/{1}".format(SHARED_VOLUME_PATH, workflow_workspace)
    # use some shared object between tasks.
    os.environ["workflow_uuid"] = workflow_uuid
    os.environ["workflow_workspace"] = workflow_workspace
    os.umask(REANA_WORKFLOW_UMASK)

    cap_backend = setupbackend_fromstring("fromenv")
    workflow_file_abs_path = os.path.join(workflow_workspace, workflow_file)
    publisher = REANAWorkflowStatusPublisher()
    try:
        if not os.path.exists(workflow_file_abs_path):
            message = f"Workflow file {workflow_file} does not exist"
            raise Exception(message)
        else:
            schema_name = "yadage/workflow-schema"
            schemadir = None
            specopts = {
                "toplevel": operational_options["toplevel"],
                "schema_name": schema_name,
                "schemadir": schemadir,
                "load_as_ref": False,
            }

            validopts = {
                "schema_name": schema_name,
                "schemadir": schemadir,
            }
            workflow_json = yadageschemas.load(
                spec=workflow_file,
                specopts=specopts,
                validopts=validopts,
                validate=True,
            )
            workflow_kwargs = dict(workflow_json=workflow_json)
        dataopts = {"initdir": operational_options["initdir"]}

        initdata = {}
        for initfile in operational_options["initfiles"]:
            initdata.update(**yaml.safe_load(open(initfile)))
        initdata.update(workflow_parameters)

        check_connection_to_job_controller()

        with steering_ctx(
            dataarg=workflow_workspace,
            dataopts=dataopts,
            initdata=initdata,
            visualize=True,
            updateinterval=5,
            loginterval=5,
            backend=cap_backend,
            accept_metadir="accept_metadir" in operational_options,
            **workflow_kwargs,
        ) as ys:

            log.info("running workflow on context: {0}".format(locals()))
            publisher.publish_workflow_status(workflow_uuid, 1)

            ys.adage_argument(
                additional_trackers=[REANATracker(identifier=workflow_uuid)]
            )

        publisher.publish_workflow_status(workflow_uuid, 2)

        log.info(
            "Workflow {workflow_uuid} finished. Files available "
            "at {workflow_workspace}.".format(
                workflow_uuid=workflow_uuid, workflow_workspace=workflow_workspace
            )
        )
    except Exception as e:
        log.error("Workflow failed: {0}".format(e), exc_info=True)
        if publisher:
            publisher.publish_workflow_status(
                workflow_uuid, 3, logs="workflow failed: {0}".format(e)
            )
        else:
            log.error(
                "Workflow {workflow_uuid} failed but status "
                "could not be published.".format(workflow_uuid=workflow_uuid)
            )
