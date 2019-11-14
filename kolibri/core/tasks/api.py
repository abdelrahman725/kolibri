import logging
import os
from functools import partial

from django.apps.registry import AppRegistryNotReady
from django.core.management import call_command
from django.http.response import Http404
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers
from rest_framework import status
from rest_framework import viewsets
from rest_framework.decorators import list_route
from rest_framework.response import Response
from six import string_types

from kolibri.core.content.constants.schema_versions import CURRENT_SCHEMA_VERSION
from kolibri.core.content.models import ChannelMetadata
from kolibri.core.content.models import ContentNode
from kolibri.core.content.permissions import CanExportLogs
from kolibri.core.content.permissions import CanManageContent
from kolibri.core.content.utils import annotation
from kolibri.core.content.utils import channel_import
from kolibri.core.content.utils import paths
from kolibri.core.content.utils.channels import get_mounted_drive_by_id
from kolibri.core.content.utils.channels import get_mounted_drives_with_channel_info
from kolibri.core.content.utils.paths import get_content_database_file_path
from kolibri.core.discovery.models import NetworkLocation
from kolibri.core.content.utils.sqlalchemybridge import Bridge
from kolibri.core.tasks.exceptions import JobNotFound
from kolibri.core.tasks.exceptions import UserCancelledError
from kolibri.core.tasks.job import State
from kolibri.core.tasks.main import queue
from kolibri.utils import conf

try:
    from django.apps import apps

    apps.check_apps_ready()
except AppRegistryNotReady:
    import django

    django.setup()

logger = logging.getLogger(__name__)


NETWORK_ERROR_STRING = _("There was a network error.")

DISK_IO_ERROR_STRING = _("There was a disk access error.")

CATCHALL_SERVER_ERROR_STRING = _("There was an unknown error.")


def validate_content_task(request, task_description, require_channel=False):
    try:
        channel_id = task_description["channel_id"]
    except KeyError:
        raise serializers.ValidationError("The channel_ids field is required.")

    try:
        channel = ChannelMetadata.objects.get(id=channel_id)
        channel_name = channel.name
        file_size = channel.published_size
        total_resources = channel.total_resource_count
    except ChannelMetadata.DoesNotExist:
        if require_channel:
            raise serializers.ValidationError("This channel does not exist")
        channel_name = ""
        file_size = None
        total_resources = None

    file_size = task_description.get("file_size", file_size)

    total_resources = task_description.get("total_resources", total_resources)

    node_ids = task_description.get("node_ids", None)
    exclude_node_ids = task_description.get("exclude_node_ids", None)

    if node_ids and not isinstance(node_ids, list):
        raise serializers.ValidationError("node_ids must be a list.")

    if exclude_node_ids and not isinstance(exclude_node_ids, list):
        raise serializers.ValidationError("exclude_node_ids must be a list.")

    return {
        "channel_id": channel_id,
        "channel_name": channel_name,
        "file_size": file_size,
        "total_resources": total_resources,
        "exclude_node_ids": exclude_node_ids,
        "node_ids": node_ids,
        "started_by": request.user.pk,
        "started_by_username": request.user.username,
    }


def validate_remote_import_task(request, task_description):
    import_task = validate_content_task(request, task_description)
    try:
        peer_id = task_description["peer_id"]
        baseurl = NetworkLocation.objects.values_list("base_url", flat=True).get(
            id=peer_id
        )
    except NetworkLocation.DoesNotExist:
        raise serializers.ValidationError(
            "Peer with id {} does not exist".format(peer_id)
        )
    except KeyError:
        baseurl = conf.OPTIONS["Urls"]["CENTRAL_CONTENT_BASE_URL"]
        peer_id = None

    import_task.update({"baseurl": baseurl, "peer_id": peer_id})
    return import_task


def _add_drive_info(import_task, task_description):
    try:
        drive_id = task_description["drive_id"]
    except KeyError:
        raise serializers.ValidationError("The drive_id field is required.")

    try:
        drive = get_mounted_drive_by_id(drive_id)
    except KeyError:
        raise serializers.ValidationError(
            "That drive_id was not found in the list of drives."
        )

    import_task.update({"drive_id": drive_id, "datafolder": drive.datafolder})

    return import_task


def validate_local_import_task(request, task_description):
    task = validate_content_task(request, task_description)
    task = _add_drive_info(task, task_description)
    return task


def validate_local_export_task(request, task_description):
    task = validate_content_task(request, task_description, require_channel=True)
    task = _add_drive_info(task, task_description)
    return task


def validate_deletion_task(request, task_description):
    task = validate_content_task(request, task_description, require_channel=True)
    task["force_delete"] = bool(task_description.get("force_delete"))
    return task


class TasksViewSet(viewsets.ViewSet):
    def get_permissions(self):
        # task permissions shared between facility management and device management
        if self.action in ["list", "deletefinishedtasks"]:
            permission_classes = [CanManageContent | CanExportLogs]
        # exclusive permission for facility management
        elif self.action == "startexportlogcsv":
            permission_classes = [CanExportLogs]
        # this was the default before, so leave as is for any other endpoints
        else:
            permission_classes = [CanManageContent]
        return [permission() for permission in permission_classes]

    def list(self, request):
        jobs_response = [_job_to_response(j) for j in queue.jobs]

        return Response(jobs_response)

    def create(self, request):
        # unimplemented. Call out to the task-specific APIs for now.
        pass

    def retrieve(self, request, pk=None):
        try:
            task = _job_to_response(queue.fetch_job(pk))
            return Response(task)
        except JobNotFound:
            raise Http404("Task with {pk} not found".format(pk=pk))

    def destroy(self, request, pk=None):
        # unimplemented for now.
        pass

    @list_route(methods=["post"])
    def startremotebulkimport(self, request):
        if not isinstance(request.data, list):
            raise serializers.ValidationError(
                "POST data must be a list of task descriptions"
            )

        tasks = map(partial(validate_remote_import_task, request), request.data)

        job_ids = []

        for task in tasks:
            task.update({"type": "REMOTEIMPORT"})
            import_job_id = queue.enqueue(
                _remoteimport,
                task["channel_id"],
                task["baseurl"],
                peer_id=task["peer_id"],
                extra_metadata=task,
                cancellable=True,
            )
            job_ids.append(import_job_id)

        resp = [_job_to_response(queue.fetch_job(job_id)) for job_id in job_ids]

        return Response(resp)

    @list_route(methods=["post"])
    def startremotechannelimport(self, request):

        task = validate_remote_import_task(request, request.data)

        task.update({"type": "REMOTECHANNELIMPORT"})

        job_id = queue.enqueue(
            call_command,
            "importchannel",
            "network",
            task["channel_id"],
            baseurl=task["baseurl"],
            peer_id=task["peer_id"],
            extra_metadata=task,
            cancellable=True,
        )
        resp = _job_to_response(queue.fetch_job(job_id))

        return Response(resp)

    @list_route(methods=["post"])
    def startremotecontentimport(self, request):

        task = validate_remote_import_task(request, request.data)
        task.update({"type": "REMOTECONTENTIMPORT"})

        job_id = queue.enqueue(
            call_command,
            "importcontent",
            "network",
            task["channel_id"],
            baseurl=task["baseurl"],
            peer_id=task["peer_id"],
            node_ids=task["node_ids"],
            exclude_node_ids=task["exclude_node_ids"],
            extra_metadata=task,
            track_progress=True,
            cancellable=True,
        )

        resp = _job_to_response(queue.fetch_job(job_id))

        return Response(resp)

    @list_route(methods=["post"])
    def startdiskbulkimport(self, request):
        if not isinstance(request.data, list):
            raise serializers.ValidationError(
                "POST data must be a list of task descriptions"
            )

        tasks = map(partial(validate_local_import_task, request), request.data)

        job_ids = []

        for task in tasks:
            task.update({"type": "DISKIMPORT"})
            import_job_id = queue.enqueue(
                _diskimport,
                task["channel_id"],
                task["datafolder"],
                drive_id=task["drive_id"],
                extra_metadata=task,
                track_progress=True,
                cancellable=True,
            )
            job_ids.append(import_job_id)

        resp = [_job_to_response(queue.fetch_job(job_id)) for job_id in job_ids]

        return Response(resp)

    @list_route(methods=["post"])
    def startdiskchannelimport(self, request):
        task = validate_local_import_task(request, request.data)

        task.update({"type": "DISKCHANNELIMPORT"})

        job_id = queue.enqueue(
            call_command,
            "importchannel",
            "disk",
            task["channel_id"],
            task["datafolder"],
            drive_id=task["drive_id"],
            extra_metadata=task,
            cancellable=True,
        )

        resp = _job_to_response(queue.fetch_job(job_id))
        return Response(resp)

    @list_route(methods=["post"])
    def startdiskcontentimport(self, request):
        task = validate_local_import_task(request, request.data)

        task.update({"type": "DISKCONTENTIMPORT"})

        job_id = queue.enqueue(
            call_command,
            "importcontent",
            "disk",
            task["channel_id"],
            task["datafolder"],
            drive_id=task["drive_id"],
            node_ids=task["node_ids"],
            exclude_node_ids=task["exclude_node_ids"],
            extra_metadata=task,
            track_progress=True,
            cancellable=True,
        )

        resp = _job_to_response(queue.fetch_job(job_id))

        return Response(resp)

    @list_route(methods=["post"])
    def startbulkdelete(self, request):
        if not isinstance(request.data, list):
            raise serializers.ValidationError(
                "POST data must be a list of task descriptions"
            )

        tasks = map(partial(validate_deletion_task, request), request.data)

        job_ids = []

        for task in tasks:
            task.update({"type": "DELETECHANNEL"})
            if task["node_ids"] or task["exclude_node_ids"]:
                task["file_size"] = None
                task["total_resources"] = None
            delete_job_id = queue.enqueue(
                call_command,
                "deletecontent",
                task["channel_id"],
                track_progress=True,
                extra_metadata=task,
            )
            job_ids.append(delete_job_id)

        resp = [_job_to_response(queue.fetch_job(job_id)) for job_id in job_ids]

        return Response(resp)

    @list_route(methods=["post"])
    def startdeletechannel(self, request):
        """
        Delete a channel and all its associated content from the server
        """
        task = validate_deletion_task(request, request.data)

        task.update({"type": "DELETECONTENT"})

        if task["node_ids"] or task["exclude_node_ids"]:
            task["file_size"] = None
            task["total_resources"] = None

        task_id = queue.enqueue(
            call_command,
            "deletecontent",
            task["channel_id"],
            node_ids=task["node_ids"],
            exclude_node_ids=task["exclude_node_ids"],
            force_delete=task["force_delete"],
            track_progress=True,
            extra_metadata=task,
        )

        # attempt to get the created Task, otherwise return pending status
        resp = _job_to_response(queue.fetch_job(task_id))

        return Response(resp)

    @list_route(methods=["post"])
    def startdiskbulkexport(self, request):
        if not isinstance(request.data, list):
            raise serializers.ValidationError(
                "POST data must be a list of task descriptions"
            )

        tasks = map(partial(validate_local_export_task, request), request.data)

        job_ids = []

        for task in tasks:
            task.update({"type": "DISKEXPORT"})
            export_job_id = queue.enqueue(
                _localexport,
                task["channel_id"],
                task["drive_id"],
                track_progress=True,
                cancellable=True,
                extra_metadata=task,
            )
            job_ids.append(export_job_id)

        resp = [_job_to_response(queue.fetch_job(job_id)) for job_id in job_ids]

        return Response(resp)

    @list_route(methods=["post"])
    def startdiskexport(self, request):
        """
        Export a channel to a local drive, and copy content to the drive.

        """

        task = validate_local_export_task(request, request.data)

        task.update({"type": "DISKCONTENTEXPORT"})

        task_id = queue.enqueue(
            _localexport,
            task["channel_id"],
            task["drive_id"],
            track_progress=True,
            cancellable=True,
            node_ids=task["node_ids"],
            exclude_node_ids=task["exclude_node_ids"],
            extra_metadata=task,
        )

        # attempt to get the created Task, otherwise return pending status
        resp = _job_to_response(queue.fetch_job(task_id))

        return Response(resp)

    @list_route(methods=["post"])
    def startdataportalsync(self, request):
        """
        Initiate a PUSH sync with Kolibri Data Portal.

        """
        task = {
            "facility": request.data["facility"],
            "type": "SYNCDATAPORTAL",
            "started_by": request.user.pk,
        }

        job_id = queue.enqueue(
            call_command,
            "sync",
            facility=task["facility"],
            noninteractive=True,
            extra_metadata=task,
            track_progress=False,
            cancellable=False,
        )
        # attempt to get the created Task, otherwise return pending status
        resp = _job_to_response(queue.fetch_job(job_id))

        return Response(resp)

    @list_route(methods=["post"])
    def canceltask(self, request):
        """
        Cancel a task with its task id given in the task_id parameter.
        """

        if "task_id" not in request.data:
            raise serializers.ValidationError("The 'task_id' field is required.")
        if not isinstance(request.data["task_id"], string_types):
            raise serializers.ValidationError("The 'task_id' should be a string.")
        try:
            queue.cancel(request.data["task_id"])
        except JobNotFound:
            pass

        return Response({})

    @list_route(methods=["post"])
    def cleartasks(self, request):
        """
        Cancels all running tasks.
        """

        queue.empty()
        return Response({})

    @list_route(methods=["post"])
    def cleartask(self, request):
        # Given a single task ID, clear it from the queue
        task_id = request.data.get("task_id")
        if task_id:
            queue.clear_job(task_id)
            return Response({"task_id": task_id})
        else:
            return Response({})

    @list_route(methods=["post"])
    def deletefinishedtasks(self, request):
        """
        Delete all tasks that have succeeded, failed, or been cancelled.
        """
        task_id = request.data.get("task_id")
        if task_id:
            queue.clear_job(task_id)
        else:
            queue.clear()
        return Response({})

    @list_route(methods=["get"])
    def localdrive(self, request):
        drives = get_mounted_drives_with_channel_info()

        # make sure everything is a dict, before converting to JSON
        assert isinstance(drives, dict)
        out = [mountdata._asdict() for mountdata in drives.values()]

        return Response(out)

    @list_route(methods=["post"])
    def startexportlogcsv(self, request):
        """
        Dumps in csv format the required logs.
        By default it will be dump contentsummarylog.

        :param: logtype: Kind of log to dump, summary or session
        :returns: An object with the job information

        """
        csv_export_filenames = {
            "session": "content_session_logs.csv",
            "summary": "content_summary_logs.csv",
        }
        log_type = request.data.get("logtype", "summary")
        if log_type in csv_export_filenames.keys():
            logs_dir = os.path.join(conf.KOLIBRI_HOME, "log_export")
            filepath = os.path.join(logs_dir, csv_export_filenames[log_type])
        else:
            raise Http404(
                "Impossible to create a csv export file for {}".format(log_type)
            )
        if not os.path.isdir(logs_dir):
            os.mkdir(logs_dir)

        job_type = (
            "EXPORTSUMMARYLOGCSV" if log_type == "summary" else "EXPORTSESSIONLOGCSV"
        )

        job_metadata = {"type": job_type, "started_by": request.user.pk}

        job_id = queue.enqueue(
            call_command,
            "exportlogs",
            log_type=log_type,
            output_file=filepath,
            overwrite="true",
            extra_metadata=job_metadata,
            track_progress=True,
        )

        resp = _job_to_response(queue.fetch_job(job_id))

        return Response(resp)

    @list_route(methods=["post"])
    def channeldiffstats(self, request):
        """
        Download the channel database to an upgraded path.
        Annotate the local file availability of the upgraded channel db.
        Calculate diff stats comparing default db and annotated channel db.
        """
        channel_id = request.data.get("channel_id")
        method = request.data.get("method")
        drive_id = request.data.get("drive_id")
        if not channel_id:
            raise serializers.ValidationError("The channel_id field is required.")
        if not method:
            raise serializers.ValidationError("The method field is required.")
        if method == "network":
            call_command(
                "importchannel",
                "network",
                channel_id,
                baseurl=request.data.get(
                    "baseurl", conf.OPTIONS["Urls"]["CENTRAL_CONTENT_BASE_URL"]
                ),
                upgrade=True,
            )
        elif method == "disk":
            if not drive_id:
                raise serializers.ValidationError(
                    "The drive_id field is required when using 'disk' method."
                )
            drive = get_mounted_drive_by_id(drive_id)
            call_command(
                "importchannel", "disk", channel_id, drive, upgrade=True,
            )
        else:
            raise serializers.ValidationError(
                "'method' field should either be 'network' or 'disk'."
            )
        # upgraded content database path
        source_path = paths.get_upgrade_content_database_file_path(channel_id)
        # annotated db to be used for calculating diff stats
        destination_path = paths.get_annotated_content_database_file_path(channel_id)
        try:
            # create all fields/tables at the annotated destination db, based on the current schema version
            bridge = Bridge(
                sqlite_file_path=destination_path, schema_version=CURRENT_SCHEMA_VERSION
            )
            bridge.Base.metadata.create_all(bridge.engine)

            # initialize import manager based on annotated destination path, pulling from source db path
            import_manager = channel_import.initialize_import_manager(
                channel_id,
                cancel_check=False,
                source=source_path,
                destination=destination_path,
            )

            # import channel data from source db path
            import_manager.import_channel_data()
            import_manager.end()

            # annotate file availability on destination db
            annotation.set_local_file_availability_from_disk(
                destination=destination_path
            )
            # get all leaf node ids on the default db
            all_leaf_node_ids = (
                ContentNode.objects.filter(channel_id=channel_id)
                .exclude(kind="topic")
                .values_list("id", flat=True)
            )
            # get the diff count between whats on the default db and the annotated db
            new_resources_count = annotation.count_new_resources_available_for_import(
                destination_path, all_leaf_node_ids,
            )
            # get available leaf node ids on the default db
            available_leaf_node_ids = (
                ContentNode.objects.filter(channel_id=channel_id, available=True)
                .exclude(kind="topic")
                .values_list("id", flat=True)
            )
            # get the count for leaf nodes which are in the default db, but not in the annotated db
            resources_to_be_deleted_count = annotation.count_missing_resources(
                destination_path, available_leaf_node_ids,
            )
            # get the ids of leaf nodes which are now incomplete due to missing local files
            updated_resources_ids = annotation.automatically_updated_resource_ids(
                destination_path, available_leaf_node_ids,
            )
            data = {
                "new_resources_count": new_resources_count,
                "deleted_resources_count": resources_to_be_deleted_count,
                "updated_node_ids": updated_resources_ids,
            }
            # remove the annotated database
            try:
                os.remove(destination_path)
            except OSError as e:
                logger.info(
                    "Tried to remove {}, but exception {} occurred.".format(
                        destination_path, e
                    )
                )
        except (
            channel_import.InvalidSchemaVersionError,
            channel_import.FutureSchemaError,
        ) as e:
            return Response(e, status=status.HTTP_400_BAD_REQUEST)
        return Response(data)


def _remoteimport(
    channel_id,
    baseurl,
    peer_id=None,
    update_progress=None,
    check_for_cancel=None,
    node_ids=None,
    exclude_node_ids=None,
    extra_metadata=None,
):

    call_command(
        "importchannel",
        "network",
        channel_id,
        baseurl=baseurl,
        update_progress=update_progress,
        check_for_cancel=check_for_cancel,
    )
    call_command(
        "importcontent",
        "network",
        channel_id,
        baseurl=baseurl,
        peer_id=peer_id,
        node_ids=node_ids,
        exclude_node_ids=exclude_node_ids,
        update_progress=update_progress,
        check_for_cancel=check_for_cancel,
    )


def _diskimport(
    channel_id,
    directory,
    drive_id=None,
    update_progress=None,
    check_for_cancel=None,
    node_ids=None,
    exclude_node_ids=None,
    extra_metadata=None,
):

    call_command(
        "importchannel",
        "disk",
        channel_id,
        directory,
        update_progress=update_progress,
        check_for_cancel=check_for_cancel,
    )
    call_command(
        "importcontent",
        "disk",
        channel_id,
        directory,
        drive_id=drive_id,
        node_ids=node_ids,
        exclude_node_ids=exclude_node_ids,
        update_progress=update_progress,
        check_for_cancel=check_for_cancel,
    )


def _localexport(
    channel_id,
    drive_id,
    update_progress=None,
    check_for_cancel=None,
    node_ids=None,
    exclude_node_ids=None,
    extra_metadata=None,
):
    drive = get_mounted_drive_by_id(drive_id)

    call_command(
        "exportchannel",
        channel_id,
        drive.datafolder,
        update_progress=update_progress,
        check_for_cancel=check_for_cancel,
    )
    try:
        call_command(
            "exportcontent",
            channel_id,
            drive.datafolder,
            node_ids=node_ids,
            exclude_node_ids=exclude_node_ids,
            update_progress=update_progress,
            check_for_cancel=check_for_cancel,
        )
    except UserCancelledError:
        try:
            os.remove(
                get_content_database_file_path(channel_id, datafolder=drive.datafolder)
            )
        except OSError:
            pass
        raise


def _job_to_response(job):
    if not job:
        return {
            "type": None,
            "started_by": None,
            "status": State.SCHEDULED,
            "percentage": 0,
            "progress": [],
            "id": None,
            "cancellable": False,
        }
    else:
        output = {
            "status": job.state,
            "exception": str(job.exception),
            "traceback": str(job.traceback),
            "percentage": job.percentage_progress,
            "id": job.job_id,
            "cancellable": job.cancellable,
        }
        output.update(job.extra_metadata)
        return output
