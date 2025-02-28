import logging
import random
import uuid

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import connections
from le_utils.constants import content_kinds
from le_utils.constants import file_formats
from le_utils.constants import format_presets
from le_utils.constants import languages
from le_utils.constants import mastery_criteria
from le_utils.constants.labels import learning_activities
from le_utils.constants.labels.levels import LEVELSLIST
from le_utils.constants.labels.needs import NEEDSLIST
from le_utils.constants.labels.subjects import SUBJECTSLIST

from kolibri.core.content.apps import KolibriContentConfig
from kolibri.core.content.models import AssessmentMetaData
from kolibri.core.content.models import ChannelMetadata
from kolibri.core.content.models import ContentNode
from kolibri.core.content.models import ContentTag
from kolibri.core.content.models import File
from kolibri.core.content.models import Language
from kolibri.core.content.models import LocalFile


logger = logging.getLogger(__name__)

# we are using a set in case we accidentally tried deleting the same object twice
generated_objects = set()

tags_generated = []

# not used in kolibri yet
IGNORED_KINDS = ["quiz", "zim"]

ALL_RESOURCES_KINDS = [
    kind.id
    for kind in content_kinds.KINDLIST
    if kind.id not in IGNORED_KINDS and kind.id != "topic"
]

LICENSE_NAME = "testing license"
LICENSE_NAME_DESCRIPTION = (
    "ABC organization authorizes kolibri to use this these resources"
)
LICENSE_OWNER = "ABC org"
MIN_SCHEMA_VERSION = 1
DEVELOPER_NAME = "bedo khaled"


# takes much time to migrate, alternatives ?
def switch_to_memory():
    logger.info("\n initializing the testing environment in memory....\n")
    for db in settings.DATABASES:
        settings.DATABASES[db] = {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": ":memory:",
        }
        try:
            del connections[db]
        except AttributeError:
            pass
        call_command("migrate", interactive=False, database=db)


# for returning random choices
def choices(sequence, k):
    return [random.choice(sequence) for _ in range(0, k)]


# format_presets.PRESETLIST to a dictionary for convenient access
format_prestets_data = {}

for format_object in format_presets.PRESETLIST:
    if format_object.kind:
        format_prestets_data[format_object.id] = format_object


# purpose : if we have a node of certain kind what type of main_file_preset (not supplementary) should map to that node
content_kind_to_main_file_preset = {
    content_kinds.VIDEO: [format_presets.VIDEO_LOW_RES, format_presets.VIDEO_HIGH_RES],
    content_kinds.AUDIO: [format_presets.AUDIO],
    content_kinds.EXERCISE: [format_presets.EXERCISE, format_presets.QTI_ZIP],
    content_kinds.DOCUMENT: [format_presets.DOCUMENT, format_presets.EPUB],
    content_kinds.HTML5: [format_presets.HTML5_ZIP],
    content_kinds.SLIDESHOW: [
        format_presets.SLIDESHOW_IMAGE,
        format_presets.SLIDESHOW_MANIFEST,
    ],
    content_kinds.H5P: [format_presets.H5P_ZIP],
    content_kinds.TOPIC: [format_presets.TOPIC_THUMBNAIL],
}

# purpose : generates thumbnail preset along with the main file preset (both map to the same node)
main_file_preset_to_thumbnail_preset = {
    # two exceptions as these file_preset are very common together
    format_presets.SLIDESHOW_IMAGE: [
        format_presets.SLIDESHOW_THUMBNAIL,
        format_presets.SLIDESHOW_MANIFEST,
    ],
    format_presets.SLIDESHOW_MANIFEST: [format_presets.SLIDESHOW_IMAGE],
    format_presets.VIDEO_LOW_RES: [format_presets.VIDEO_THUMBNAIL],
    format_presets.VIDEO_HIGH_RES: [format_presets.VIDEO_THUMBNAIL],
    format_presets.AUDIO: [format_presets.AUDIO_THUMBNAIL],
    format_presets.EXERCISE: [format_presets.EXERCISE_THUMBNAIL],
    format_presets.ZIM: [format_presets.ZIM_THUMBNAIL],
    format_presets.HTML5_ZIP: [format_presets.HTML5_THUMBNAIL],
    format_presets.H5P_ZIP: [format_presets.H5P_THUMBNAIL],
    format_presets.QTI_ZIP: [format_presets.QTI_THUMBNAIL],
    format_presets.DOCUMENT: [format_presets.DOCUMENT_THUMBNAIL],
    format_presets.EPUB: [format_presets.DOCUMENT_THUMBNAIL],
}


def generate_some_tags():

    # dummy tag names
    TAG_NAMES = [
        "Math",
        "science_related",
        "have_fun",
        "children",
        "experiment",
        "bedo_tag",
        "course",
        "culture",
        "introduction",
        "whatever",
        "another_tag",
        "nice tag",
    ]

    for tag_name in TAG_NAMES:

        tag = ContentTag.objects.create(tag_name=tag_name, id=uuid.uuid4().hex)
        tags_generated.append(tag)
        generated_objects.add(tag)


def get_or_generate_language(lang_id):
    try:
        return Language.objects.get(id=lang_id)

    except Language.DoesNotExist:

        # fetched languages from le_utils/resources/languagelookup.json
        fetched_lang_data = languages.getlang(lang_id)

        if not fetched_lang_data:
            return None
        new_lang = Language.objects.create(
            id=lang_id,
            lang_code=fetched_lang_data.primary_code,
            lang_subcode=fetched_lang_data.subcode,
            lang_name=fetched_lang_data.native_name,
            lang_direction=languages.getlang_direction(lang_id),
        )

        generated_objects.add(new_lang)

        return new_lang


def generate_assessmentmetadata(node):
    number_of_assessments = random.randint(10, 35)
    assessment_item_ids = [str(uuid.uuid4().hex) for _ in range(number_of_assessments)]

    random_criteria = random.choice(mastery_criteria.MASTERYCRITERIALIST)

    mapper = {
        mastery_criteria.DO_ALL: {
            "type": random_criteria,
            "n": number_of_assessments,
            "m": number_of_assessments,
        },
        mastery_criteria.NUM_CORRECT_IN_A_ROW_10: {
            "type": random_criteria,
            "n": 10,
            "m": 10,
        },
        mastery_criteria.NUM_CORRECT_IN_A_ROW_2: {
            "type": random_criteria,
            "n": 2,
            "m": 2,
        },
        mastery_criteria.NUM_CORRECT_IN_A_ROW_3: {
            "type": random_criteria,
            "n": 3,
            "m": 3,
        },
        mastery_criteria.NUM_CORRECT_IN_A_ROW_5: {
            "type": random_criteria,
            "n": 5,
            "m": 5,
        },
        mastery_criteria.M_OF_N: {
            "type": random_criteria,
            "n": random.randint(5, 7),
            "m": random.randint(1, 3),
        },
    }

    meta_data = AssessmentMetaData.objects.create(
        id=uuid.uuid4().hex,
        contentnode=node,
        assessment_item_ids=assessment_item_ids,
        number_of_assessments=number_of_assessments,
        mastery_model=mapper[random_criteria],
        randomize=random.choice([True, False]),
        is_manipulable=random.choice([True, False]),
    )
    generated_objects.add(meta_data)
    return meta_data


def generate_channel(name, root_node, channel_id):

    channel = ChannelMetadata.objects.create(
        id=channel_id,
        name=name,
        description="Testing channel generated by Bedo",
        author=DEVELOPER_NAME,
        min_schema_version=MIN_SCHEMA_VERSION,
        root=root_node,
    )

    return channel


def generate_localfile(file_preset):

    # precalculated by taking the average file_size of actual existing localfiles
    # instead of just random generated sizes, these are more relvant to the corresponding extension
    extension_to_file_size = {
        file_formats.MP4: 5933249,
        file_formats.WEBM: None,
        file_formats.VTT: 1242,
        file_formats.PDF: 6655360,
        file_formats.EPUB: 13291472,
        file_formats.MP3: 2102685,
        file_formats.JPG: 5604683,
        file_formats.JPEG: 30433803,
        file_formats.PNG: 609113,
        file_formats.GIF: None,
        file_formats.JSON: 3529,
        file_formats.SVG: None,
        file_formats.GRAPHIE: None,
        file_formats.PERSEUS: 131841,
        file_formats.H5P: 10699889,
        file_formats.ZIM: None,
        file_formats.HTML5: 1315774,
    }

    extensions_choices = format_prestets_data[file_preset].allowed_formats

    extension_to_use = random.choice(extensions_choices)

    new_localfile = LocalFile.objects.create(
        id=uuid.uuid4().hex,
        extension=extension_to_use,
        available=True,
        file_size=extension_to_file_size[extension_to_use],
    )

    generated_objects.add(new_localfile)
    return new_localfile


def generate_file(contentnode):

    main_preset = random.choice(content_kind_to_main_file_preset[contentnode.kind])

    # generating the thumbnail_preset file (not supplementary preset)
    # aka checking if it's not a prest of 'topic' node (e.g. topic_thumbnail)
    if main_preset in main_file_preset_to_thumbnail_preset:

        thumbnail_preset = random.choice(
            main_file_preset_to_thumbnail_preset[main_preset]
        )

        File.objects.create(
            id=uuid.uuid4().hex,
            local_file=generate_localfile(thumbnail_preset),
            contentnode=contentnode,
            lang=contentnode.lang,
            supplementary=format_prestets_data[thumbnail_preset].supplementary,
            thumbnail=format_prestets_data[thumbnail_preset].thumbnail,
            preset=thumbnail_preset,
        )

    # generating the main_preset file (most probably a renderable resource)
    File.objects.create(
        id=uuid.uuid4().hex,
        local_file=generate_localfile(main_preset),
        contentnode=contentnode,
        lang=contentnode.lang,
        supplementary=format_prestets_data[main_preset].supplementary,
        thumbnail=format_prestets_data[main_preset].thumbnail,
        preset=main_preset,
    )


def generate_one_contentNode(
    kind,
    title,
    channel_id,
    description="",
    parent=None,
    available=True,
    lang_id="en",
    node_tags=[],
):

    kind_to_learninactivity = {
        content_kinds.TOPIC: "",
        content_kinds.SLIDESHOW: "",
        content_kinds.DOCUMENT: "{},{}".format(
            learning_activities.READ, learning_activities.REFLECT
        ),
        content_kinds.VIDEO: "{},{}".format(
            learning_activities.WATCH, learning_activities.REFLECT
        ),
        content_kinds.HTML5: "{},{}".format(
            learning_activities.EXPLORE, learning_activities.REFLECT
        ),
        content_kinds.AUDIO: "{},{}".format(
            learning_activities.LISTEN, learning_activities.REFLECT
        ),
        content_kinds.EXERCISE: "{},{}".format(
            learning_activities.PRACTICE, learning_activities.REFLECT
        ),
        content_kinds.H5P: "{}.{}".format(
            learning_activities.EXPLORE, learning_activities.REFLECT
        ),
    }

    new_node = ContentNode.objects.create(
        id=uuid.uuid4().hex,
        parent=parent,
        channel_id=channel_id,
        content_id=uuid.uuid4().hex,
        kind=kind,
        title=title,
        lang=get_or_generate_language(lang_id),
        license_name=LICENSE_NAME,
        license_description=LICENSE_NAME_DESCRIPTION,
        description=description,
        license_owner=LICENSE_OWNER,
        author=DEVELOPER_NAME,
        available=available,
        learning_activities=kind_to_learninactivity[kind],
        categories=",".join(set(choices(SUBJECTSLIST, k=random.randint(1, 10)))),
        learner_needs=",".join(set(choices(NEEDSLIST, k=random.randint(1, 5)))),
        grade_levels=",".join(set(choices(LEVELSLIST, k=random.randint(1, 2)))),
    )

    if node_tags:
        new_node.tags.add(*node_tags)
        new_node.save()

    # generate related File object for this node
    generate_file(new_node)

    # generate assessment metada for this contentnode if its kind is exercise, correct or no?
    if kind == content_kinds.EXERCISE:
        generate_assessmentmetadata(node=new_node)

    return new_node


def generate_topic(parent=None, title="topic node", channel_id=None, description=""):
    return generate_one_contentNode(
        kind=content_kinds.TOPIC,
        title=title,
        channel_id=channel_id,
        parent=parent,
        description=description,
    )


def generate_leaf(
    parent,
    resource_kind,
    channel_id,
    description="",
):
    return generate_one_contentNode(
        kind=resource_kind,
        title="{} resource".format(resource_kind),
        channel_id=channel_id,
        parent=parent,
        description=description,
        node_tags=random.sample(tags_generated, random.randint(1, 5)),
    )


def recurse_and_generate(channel_id, parent, levels, n_children, resources_kind):
    children = []
    for i in range(n_children):
        if levels == 0:
            node = generate_leaf(
                parent=parent,
                channel_id=channel_id,
                resource_kind=resources_kind
                if resources_kind
                else random.choice(ALL_RESOURCES_KINDS),
            )
        else:
            node = generate_topic(
                parent=parent,
                channel_id=channel_id,
                title="Level_{} Topic_{}".format(levels, i + 1),
            )
            node.children.add(
                *recurse_and_generate(
                    channel_id=channel_id,
                    parent=parent,
                    levels=levels - 1,
                    n_children=n_children,
                    resources_kind=resources_kind,
                )
            )
        children.append(node)
    return children


def generate_channels(n_channels=1, levels=2, n_children=3, resources_kind=None):

    generated_channels = []

    logger.info(
        "\n generating Channel/s of {} resources ...\n".format(
            resources_kind if resources_kind else "random"
        )
    )

    generate_some_tags()

    for c in range(n_channels):

        channel_id = uuid.uuid4().hex

        root_node = generate_topic(
            title="Root Node of Channel_{}".format(c + 1),
            channel_id=channel_id,
            description="First Node of channel tree",
        )

        channel = generate_channel(
            name="Testing Channel_{}".format(c + 1),
            root_node=root_node,
            channel_id=channel_id,
        )

        root_node.children.add(
            *recurse_and_generate(
                channel_id=channel_id,
                parent=root_node,
                levels=levels,
                n_children=n_children,
                resources_kind=resources_kind,
            )
        )

        channel_contents = ContentNode.objects.filter(
            channel_id=channel_id,
        ).exclude(kind=content_kinds.TOPIC)

        channel.total_resource_count = channel_contents.count()

        for each_content in channel_contents:
            if each_content.lang:
                channel.included_languages.add(each_content.lang)

        generated_channels.append(channel)

    return generated_channels


class Command(BaseCommand):

    help = "generate fixtures/data for the specified app"

    def add_arguments(self, parser):

        parser.add_argument(
            "--mode",
            type=str,
            choices=["fixtures", "default_db"],
            default="default_db",
            help="data destination after generation, dumped into fixtures and deleted, or saved in default db",
        )

        parser.add_argument(
            "--fixtures_path",
            type=str,
        )

        parser.add_argument("--seed", type=int, default=1, help="Random seed")

        parser.add_argument(
            "--channels",
            type=int,
            choices=range(1, 10),
            default=1,
            help="number of tree levels",
        )

        parser.add_argument(
            "--levels",
            type=int,
            choices=range(1, 10),
            default=2,
            help="number of tree levels",
        )

        parser.add_argument(
            "--children",
            type=int,
            choices=range(1, 10),
            default=3,
            help="number of each node children",
        )

        parser.add_argument("--resources_kind", type=str, choices=ALL_RESOURCES_KINDS)

    def handle(self, *args, **options):

        seed_n = options["seed"]
        generating_mode = options["mode"]
        fixtures_path = options["fixtures_path"]

        n_channels = options["channels"]
        required_levels = options["levels"]
        n_children = options["children"]
        resources_kind = options["resources_kind"]

        # Set the random seed so that all operations will be randomized predictably
        random.seed(seed_n)

        if generating_mode == "fixtures":
            if not fixtures_path:
                raise ValueError(
                    "\n--fixtures_path is missing : please provide a fixtures file path"
                )

            switch_to_memory()

            channels_generated = generate_channels(
                n_channels=n_channels,
                levels=required_levels,
                n_children=n_children,
                resources_kind=resources_kind,
            )

            logger.info(
                "\ndumping and creating fixtures for facilities and its data... \n"
            )

            call_command(
                "dumpdata",
                KolibriContentConfig.label,
                indent=4,
                output=fixtures_path,
                interactive=False,
            )

            # although we are in memory (data will be cleared by default) but just in case we didn't switch to memory
            [
                each_channel.delete_content_tree_and_files()
                for each_channel in channels_generated
            ]

            [
                each_generated_object.delete()
                for each_generated_object in generated_objects
            ]

        else:
            generate_channels(
                n_channels=n_channels,
                levels=required_levels,
                n_children=n_children,
                resources_kind=resources_kind,
            )
        logger.info("\n done \n")
