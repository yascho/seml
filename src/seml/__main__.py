#!/usr/bin/env python
import functools
import json
import logging
import os
import sys
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple, TypeVar

from typing_extensions import Annotated, ParamSpec

import seml.typer as typer
from seml.add import add_config_files
from seml.configure import configure
from seml.database import (
    clean_unreferenced_artifacts,
    get_collections_from_mongo_shell_or_pymongo,
    get_mongodb_config,
)
from seml.description import (
    collection_delete_description,
    collection_list_descriptions,
    collection_set_description,
)
from seml.manage import (
    cancel_experiments,
    delete_experiments,
    detect_killed,
    drop_collections,
    list_database,
    print_duplicates,
    print_fail_trace,
    print_output,
    print_status,
    reload_sources,
    reset_experiments,
)
from seml.project import init_project, print_available_templates
from seml.settings import SETTINGS
from seml.start import print_command, start_experiments, start_jupyter_job
from seml.utils import cache_to_disk

States = SETTINGS.STATES


P = ParamSpec('P')
R = TypeVar('R')


def restrict_collection(require: bool = True):
    """Decorator to require a collection name."""

    def decorator(fun: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(fun)
        def wrapper(ctx: typer.Context, *args: P.args, **kwargs: P.kwargs):
            if require and not ctx.obj['collection']:
                raise typer.BadParameter('Please specify a collection name.', ctx=ctx)
            elif not require and ctx.obj['collection']:
                raise typer.BadParameter(
                    'Please do not specify a collection name.', ctx=ctx
                )
            return fun(ctx, *args, **kwargs)

        return wrapper

    return decorator


@cache_to_disk('db_config', SETTINGS.AUTOCOMPLETE_CACHE_ALIVE_TIME)
def db_collection_completer():
    """CLI completion for db collections."""
    config = get_mongodb_config()
    return list(get_collections_from_mongo_shell_or_pymongo(**config))


app = typer.Typer(
    no_args_is_help=True,
    # Note that this is not 100% the correct chaining autocompletition
    # but it is significantly better than nothing. Compared to the default
    # click chaining we greedly split the arguments by any command.
    chain=bool(os.environ.get('_SEML_COMPLETE')),
)
YesAnnotation = Annotated[
    bool,
    typer.Option(
        '-y',
        '--yes',
        help='Automatically confirm all dialogues with yes.',
        is_flag=True,
    ),
]
SacredIdAnnotation = Annotated[
    Optional[int],
    typer.Option(
        '-id',
        '--sacred-id',
        help='Sacred ID (_id in the database collection) of the experiment. '
        'Takes precedence over other filters.',
    ),
]
FilterDictAnnotation = Annotated[
    Optional[Dict],
    typer.Option(
        '-f',
        '--filter-dict',
        help='Dictionary (passed as a string, e.g. \'{"config.dataset": "cora_ml"}\') to filter '
        'the experiments by.',
        metavar='JSON',
        parser=json.loads,
    ),
]
BatchIdAnnotation = Annotated[
    Optional[int],
    typer.Option(
        '-b',
        '--batch-id',
        help='Batch ID (batch_id in the database collection) of the experiments. '
        'Experiments that were staged together have the same batch_id.',
    ),
]


def parse_optional_str_list(values: Optional[Sequence[str]]) -> List[str]:
    if values is None:
        return []
    return [
        __x.strip()
        for _x in values
        for __x in _x.replace(',', ' ').split()
        if __x.strip()
    ]


ProjectionAnnotation = Annotated[
    List[str],
    typer.Option(
        '-p',
        '--projection',
        help='List of configuration keys, e.g., `config.model`, to additionally print.',
        parser=str.strip,
        callback=parse_optional_str_list,
        metavar='KEY',
    ),
]

_STATE_LIST = [s for states in States.values() for s in states]
FilterStatesAnnotation = Annotated[
    List[str],
    typer.Option(
        '-s',
        '--filter-states',
        help='List of states to filter the experiments by. If empty (""), all states are considered.',
        metavar=f'[{"|".join(_STATE_LIST)}]',
        parser=lambda s: s.strip().upper(),
        callback=parse_optional_str_list,
    ),
]
SBatchOptionsAnnotation = Annotated[
    Optional[Dict],
    typer.Option(
        '-sb',
        '--sbatch-options',
        help='Dictionary (passed as a string, e.g. \'{"gres": "gpu:2"}\') to request two GPUs.',
        metavar='JSON',
        parser=json.loads,
    ),
]
NumExperimentsAnnotation = Annotated[
    int,
    typer.Option(
        '-n',
        '--num-experiments',
        help='Number of experiments to start. ' '0: all (staged) experiments ',
    ),
]
NoFileOutputAnnotation = Annotated[
    bool,
    typer.Option(
        '-nf',
        '--no-file-output',
        help="Do not write the experiment's output to a file.",
        is_flag=True,
    ),
]
OutputToConsoleAnnotation = Annotated[
    bool,
    typer.Option(
        '-o',
        '--output-to-console',
        help="Write the experiment's output to the console.",
        is_flag=True,
    ),
]
StealSlurmAnnotation = Annotated[
    bool,
    typer.Option(
        '-ss',
        '--steal-slurm',
        help="Local jobs 'steal' from the Slurm queue, "
        'i.e. also execute experiments waiting for execution via Slurm.',
        is_flag=True,
    ),
]
PostMortemAnnotation = Annotated[
    bool,
    typer.Option(
        '-pm',
        '--post-mortem',
        help='Activate post-mortem debugging with pdb.',
        is_flag=True,
    ),
]
WorkerGPUsAnnotation = Annotated[
    Optional[str],
    typer.Option(
        '-wg',
        '--worker-gpus',
        help='The IDs of the GPUs used by the local worker. Will be directly passed to CUDA_VISIBLE_DEVICES.',
    ),
]
WorkerCPUsAnnotation = Annotated[
    Optional[int],
    typer.Option(
        '-wc',
        '--worker-cpus',
        help='The number of CPUs used by the local worker. Will be directly passed to OMP_NUM_THREADS.',
    ),
]
WorkerEnvAnnotation = Annotated[
    Optional[Dict],
    typer.Option(
        '-we',
        '--worker-env',
        help='Further environment variables to be set for the local worker.',
        metavar='JSON',
        parser=json.loads,
    ),
]
PrintFullDescriptionAnnotation = Annotated[
    bool,
    typer.Option(
        '-fd',
        '--full-descriptions',
        help='Whether to print full descriptions (possibly with line breaks).',
        is_flag=True,
    ),
]
UpdateStatusAnnotation = Annotated[
    bool,
    typer.Option(
        '-u',
        '--update-status',
        help='Whether to update the status of experiments in the database. '
        'This can take a while for large collections. Use only if necessary.',
        is_flag=True,
    ),
]
NoResolveDescriptionAnnotation = Annotated[
    bool,
    typer.Option(
        '--no-resolve-descriptions',
        help='Whether to prevent using omegaconf to resolve experiment descriptions',
        is_flag=True,
    ),
]
DebugAnnotation = Annotated[
    bool,
    typer.Option(
        '-d',
        '--debug',
        help='Run a single interactive experiment without Sacred observers and with post-mortem debugging. '
        'Implies `--verbose --num-exps 1 --post-mortem --output-to-console`.',
        is_flag=True,
    ),
]

DebugServerAnnotation = Annotated[
    bool,
    typer.Option(
        '-ds',
        '--debug-server',
        help='Run the experiment with a debug server, to which you can remotely connect with e.g. VS Code. '
        'Implies `--debug`.',
        is_flag=True,
    ),
]


def version_callback(value: bool):
    if value:
        from seml import __version__

        print(__version__)
        raise typer.Exit(0)


@app.callback()
def callback(
    ctx: typer.Context,
    collection: Annotated[
        str,
        typer.Argument(
            help='The name of the database collection to use.',
            autocompletion=db_collection_completer,
        ),
    ],
    verbose: Annotated[
        bool,
        typer.Option(
            '-v',
            '--verbose',
            help='Whether to print debug messages.',
            is_flag=True,
        ),
    ] = False,
    version: Annotated[
        bool,
        typer.Option(
            '-V',
            '--version',
            help='Print the version number.',
            is_flag=True,
            callback=version_callback,
        ),
    ] = False,
):
    """SEML - Slurm Experiment Management Library."""
    from rich.logging import RichHandler

    from seml.console import console

    if len(logging.root.handlers) == 0:
        logging_level = logging.NOTSET if verbose else logging.INFO
        handler = RichHandler(
            logging_level,
            console=console,
            show_path=False,
            show_level=True,
            show_time=False,
        )
        logging.basicConfig(
            level=logging_level, format='%(message)s', handlers=[handler]
        )

    ctx.obj = dict(collection=collection, verbose=verbose)


@app.command('list')
@restrict_collection(False)
def list_command(
    ctx: typer.Context,
    pattern: Annotated[
        str, typer.Argument(help='A regex that must match the collections to print.')
    ] = r'.*',
    progress: Annotated[
        bool,
        typer.Option(
            '-p',
            '--progress',
            help='Whether to print a progress bar for iterating over collections.',
            is_flag=True,
        ),
    ] = False,
    update_status: UpdateStatusAnnotation = False,
    full_description: PrintFullDescriptionAnnotation = False,
):
    """Lists all collections in the database."""
    list_database(
        pattern,
        progress=progress,
        update_status=update_status,
        print_full_description=full_description,
    )


@app.command('clean-db')
def clean_db_command(ctx: typer.Context, yes: YesAnnotation = False):
    """Remove orphaned artifacts in the DB from runs which have been deleted.."""
    clean_unreferenced_artifacts(ctx.obj['collection'], yes=yes)


@app.command('configure')
@restrict_collection(False)
def configure_command(
    ctx: typer.Context,
    ssh_forward: Annotated[
        bool,
        typer.Option(
            '-sf',
            '--ssh-forward',
            help='Configure SSH forwarding settings for MongoDB.',
            is_flag=True,
        ),
    ] = False,
):
    """
    Configure SEML (database, argument completion, ...).
    """
    configure(all=False, mongodb=True, setup_ssh_forward=ssh_forward)


@app.command('start-jupyter')
@restrict_collection(False)
def start_jupyter_command(
    ctx: typer.Context,
    lab: Annotated[
        bool,
        typer.Option(
            '-l',
            '--lab',
            help='Start a jupyter-lab instance instead of jupyter notebook.',
        ),
    ] = False,
    conda_env: Annotated[
        Optional[str],
        typer.Option(
            '-c',
            '--conda-env',
            help='Start the Jupyter instance in a Conda environment.',
        ),
    ] = None,
    sbatch_options: SBatchOptionsAnnotation = None,
):
    """
    Start a Jupyter slurm job. Uses SBATCH options defined in settings.py under
    SBATCH_OPTIONS_TEMPLATES.JUPYTER
    """
    start_jupyter_job(lab=lab, conda_env=conda_env, sbatch_options=sbatch_options)


@app.command('cancel')
@restrict_collection()
def cancel_command(
    ctx: typer.Context,
    sacred_id: SacredIdAnnotation = None,
    filter_dict: FilterDictAnnotation = None,
    batch_id: BatchIdAnnotation = None,
    filter_states: FilterStatesAnnotation = [*States.PENDING, *States.RUNNING],
    wait: Annotated[
        bool,
        typer.Option(
            '-w',
            '--wait',
            help='Wait until all jobs are properly cancelled.',
            is_flag=True,
        ),
    ] = False,
    yes: YesAnnotation = False,
):
    """
    Cancel the Slurm job/job step corresponding to experiments, filtered by ID or state.
    """
    wait |= (
        len(
            [
                a
                for a in sys.argv
                if a in command_tree(app).commands or a in command_tree(app).groups
            ]
        )
        > 1
    )
    cancel_experiments(
        ctx.obj['collection'],
        sacred_id=sacred_id,
        filter_dict=filter_dict,
        batch_id=batch_id,
        filter_states=filter_states,
        wait=wait,
        yes=yes,
    )


@app.command('add')
@restrict_collection()
def add_command(
    ctx: typer.Context,
    config_files: Annotated[
        List[str],
        typer.Argument(
            help='Path to the YAML configuration file for the experiment.',
            exists=True,
            file_okay=True,
            dir_okay=False,
        ),
    ],
    no_hash: Annotated[
        bool,
        typer.Option(
            '-nh',
            '--no-hash',
            help='By default, we use the hash of the config dictionary to filter out duplicates (by comparing all '
            'dictionary values individually). Only disable this if you have a good reason as it is faster.',
            is_flag=True,
        ),
    ] = False,
    no_sanity_check: Annotated[
        bool,
        typer.Option(
            '-ncs',
            '--no-sanity-check',
            help='Disable this if the check fails unexpectedly when using '
            'advanced Sacred features or to accelerate adding.',
            is_flag=True,
        ),
    ] = False,
    no_code_checkpoint: Annotated[
        bool,
        typer.Option(
            '-ncc',
            '--no-code-checkpoint',
            help='Disable this if you want your experiments to use the current code'
            'instead of the code at the time of adding.',
            is_flag=True,
        ),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            '-f',
            '--force',
            help='Force adding the experiment even if it already exists in the database.',
            is_flag=True,
        ),
    ] = False,
    overwrite_params: Annotated[
        Optional[Dict],
        typer.Option(
            '-o',
            '--overwrite-params',
            help='Dictionary (passed as a string, e.g. \'{"epochs": 100}\') to overwrite parameters in the config.',
            metavar='JSON',
            parser=json.loads,
        ),
    ] = None,
    description: Annotated[
        Optional[str],
        typer.Option(
            '-d',
            '--description',
            help='A description for the experiment.',
        ),
    ] = None,
    no_resolve_descriptions: NoResolveDescriptionAnnotation = False,
):
    """
    Add experiments to the database as defined in the configuration.
    """
    add_config_files(
        ctx.obj['collection'],
        config_files,
        force_duplicates=force,
        no_hash=no_hash,
        no_sanity_check=no_sanity_check,
        no_code_checkpoint=no_code_checkpoint,
        overwrite_params=overwrite_params,
        description=description,
        resolve_descriptions=not no_resolve_descriptions,
    )


@app.command('start')
@restrict_collection()
def start_command(
    ctx: typer.Context,
    sacred_id: SacredIdAnnotation = None,
    filter_dict: FilterDictAnnotation = None,
    batch_id: BatchIdAnnotation = None,
    debug: DebugAnnotation = False,
    debug_server: DebugServerAnnotation = False,
    local: Annotated[
        bool,
        typer.Option(
            '-l',
            '--local',
            help='Run the experiment locally instead of on a Slurm cluster.',
            is_flag=True,
        ),
    ] = False,
    no_worker: Annotated[
        bool,
        typer.Option(
            '-nw',
            '--no-worker',
            help="Do not launch a local worker after setting experiments' state to PENDING.",
            is_flag=True,
        ),
    ] = False,
    num_exps: NumExperimentsAnnotation = 0,
    no_file_output: NoFileOutputAnnotation = False,
    steal_slurm: StealSlurmAnnotation = False,
    post_mortem: PostMortemAnnotation = False,
    output_to_console: OutputToConsoleAnnotation = False,
    worker_gpus: WorkerGPUsAnnotation = None,
    worker_cpus: WorkerCPUsAnnotation = None,
    worker_env: WorkerEnvAnnotation = None,
):
    """
    Fetch staged experiments from the database and run them (by default via Slurm).
    """
    start_experiments(
        ctx.obj['collection'],
        local=local,
        sacred_id=sacred_id,
        batch_id=batch_id,
        filter_dict=filter_dict,
        num_exps=num_exps,
        post_mortem=post_mortem,
        debug=debug,
        debug_server=debug_server,
        output_to_console=output_to_console,
        no_file_output=no_file_output,
        steal_slurm=steal_slurm,
        no_worker=no_worker,
        set_to_pending=True,
        worker_gpus=worker_gpus,
        worker_cpus=worker_cpus,
        worker_environment_vars=worker_env,
    )


@app.command('launch-worker')
@restrict_collection()
def launch_worker_command(
    ctx: typer.Context,
    num_exps: NumExperimentsAnnotation = 0,
    no_file_output: NoFileOutputAnnotation = False,
    steal_slurm: StealSlurmAnnotation = False,
    post_mortem: PostMortemAnnotation = False,
    debug: DebugAnnotation = False,
    debug_server: DebugServerAnnotation = False,
    output_to_console: OutputToConsoleAnnotation = False,
    worker_gpus: WorkerGPUsAnnotation = None,
    worker_cpus: WorkerCPUsAnnotation = None,
    worker_env: WorkerEnvAnnotation = None,
    sacred_id: SacredIdAnnotation = None,
    filter_dict: FilterDictAnnotation = None,
    batch_id: BatchIdAnnotation = None,
):
    """
    Launch a local worker that runs PENDING jobs.
    """
    start_experiments(
        ctx.obj['collection'],
        local=True,
        sacred_id=sacred_id,
        batch_id=batch_id,
        filter_dict=filter_dict,
        num_exps=num_exps,
        post_mortem=post_mortem,
        debug=debug,
        debug_server=debug_server,
        output_to_console=output_to_console,
        no_file_output=no_file_output,
        steal_slurm=steal_slurm,
        no_worker=False,
        set_to_pending=False,
        worker_gpus=worker_gpus,
        worker_cpus=worker_cpus,
        worker_environment_vars=worker_env,
    )


@app.command('print-fail-trace')
@restrict_collection()
def print_fail_trace_command(
    ctx: typer.Context,
    sacred_id: SacredIdAnnotation = None,
    filter_dict: FilterDictAnnotation = None,
    batch_id: BatchIdAnnotation = None,
    filter_states: FilterStatesAnnotation = [
        *States.FAILED,
        *States.KILLED,
        *States.INTERRUPTED,
    ],
    projection: ProjectionAnnotation = [],
):
    """
    Prints fail traces of all failed experiments.
    """
    print_fail_trace(
        ctx.obj['collection'],
        sacred_id=sacred_id,
        filter_states=filter_states,
        batch_id=batch_id,
        filter_dict=filter_dict,
        projection=projection,
    )


@app.command('reload-sources')
@restrict_collection()
def reload_sources_command(
    ctx: typer.Context,
    keep_old: Annotated[
        bool,
        typer.Option(
            '-k',
            '-keep-old',
            help='Keep the old source files in the database.',
            is_flag=True,
        ),
    ] = False,
    batch_ids: Annotated[
        Optional[List[int]],
        typer.Option(
            '-b',
            '--batch-ids',
            help='Batch IDs (batch_id in the database collection) of the experiments. '
            'Experiments that were staged together have the same batch_id.',
        ),
    ] = None,
    yes: YesAnnotation = False,
):
    """
    Reload stashed source files.
    """
    reload_sources(
        ctx.obj['collection'],
        batch_ids=batch_ids,
        keep_old=keep_old,
        yes=yes,
    )


@app.command('print-command')
@restrict_collection()
def print_command_command(
    ctx: typer.Context,
    sacred_id: SacredIdAnnotation = None,
    filter_states: FilterStatesAnnotation = States.STAGED,
    filter_dict: FilterDictAnnotation = None,
    batch_id: BatchIdAnnotation = None,
    num_exps: NumExperimentsAnnotation = 0,
    worker_gpus: WorkerGPUsAnnotation = None,
    worker_cpus: WorkerCPUsAnnotation = None,
    worker_env: WorkerEnvAnnotation = None,
    unresolved: Annotated[
        bool,
        typer.Option(
            '--unresolved',
            help='Whether to print the unresolved command.',
            is_flag=True,
        ),
    ] = False,
    no_interpolation: Annotated[
        bool,
        typer.Option(
            '--no-interpolation',
            help='Whether disable variable interpolation. Only compatible with --unresolved.',
            is_flag=True,
        ),
    ] = False,
):
    """
    Print the commands that would be executed by `start`.
    """
    print_command(
        ctx.obj['collection'],
        sacred_id=sacred_id,
        batch_id=batch_id,
        filter_states=filter_states,
        filter_dict=filter_dict,
        num_exps=num_exps,
        worker_gpus=worker_gpus,
        worker_cpus=worker_cpus,
        worker_environment_vars=worker_env,
        unresolved=unresolved,
        resolve_interpolations=not no_interpolation,
    )


@app.command('print-output')
@restrict_collection()
def print_output_command(
    ctx: typer.Context,
    sacred_id: SacredIdAnnotation = None,
    filter_states: FilterStatesAnnotation = States.RUNNING
    + States.FAILED
    + States.KILLED
    + States.INTERRUPTED
    + States.COMPLETED,
    filter_dict: FilterDictAnnotation = None,
    batch_id: BatchIdAnnotation = None,
):
    """
    Print the output of experiments.
    """
    print_output(
        ctx.obj['collection'],
        sacred_id=sacred_id,
        filter_states=filter_states,
        batch_id=batch_id,
        filter_dict=filter_dict,
    )


@app.command('reset')
@restrict_collection()
def reset_command(
    ctx: typer.Context,
    sacred_id: SacredIdAnnotation = None,
    filter_states: FilterStatesAnnotation = [
        *States.FAILED,
        *States.KILLED,
        *States.INTERRUPTED,
    ],
    filter_dict: FilterDictAnnotation = None,
    batch_id: BatchIdAnnotation = None,
    yes: YesAnnotation = False,
):
    """
    Reset the state of experiments by setting their state to STAGED and cleaning their database entry.
    Does not cancel Slurm jobs.
    """
    reset_experiments(
        ctx.obj['collection'],
        sacred_id=sacred_id,
        filter_states=filter_states,
        batch_id=batch_id,
        filter_dict=filter_dict,
        yes=yes,
    )


@app.command('delete')
@restrict_collection()
def delete_command(
    ctx: typer.Context,
    sacred_id: SacredIdAnnotation = None,
    filter_states: FilterStatesAnnotation = [
        *States.STAGED,
        *States.FAILED,
        *States.KILLED,
        *States.INTERRUPTED,
    ],
    filter_dict: FilterDictAnnotation = None,
    batch_id: BatchIdAnnotation = None,
    yes: YesAnnotation = False,
):
    """
    Delete experiments by ID or state (does not cancel Slurm jobs).
    """
    delete_experiments(
        ctx.obj['collection'],
        sacred_id=sacred_id,
        filter_states=filter_states,
        batch_id=batch_id,
        filter_dict=filter_dict,
        yes=yes,
    )


@app.command('drop')
@restrict_collection(False)
def drop_command(
    ctx: typer.Context,
    pattern: Annotated[
        str, typer.Argument(help='A regex that must match the collections to print.')
    ] = r'.*',
    yes: YesAnnotation = False,
):
    """
    Drop collections from the database.

    Note: This is a dangerous operation and should only be used if you know what you are doing.
    """
    drop_collections(pattern=pattern, yes=yes)


@app.command('detect-killed')
@restrict_collection()
def detect_killed_command(
    ctx: typer.Context,
):
    """
    Detect experiments where the corresponding Slurm jobs were killed externally.
    """
    detect_killed(ctx.obj['collection'])


@app.command('status')
@restrict_collection()
def status_command(
    ctx: typer.Context,
    update_status: UpdateStatusAnnotation = True,
    projection: ProjectionAnnotation = [],
):
    """
    Report status of experiments in the database collection.
    """
    print_status(
        ctx.obj['collection'], update_status=update_status, projection=projection
    )


app_description = typer.Typer(
    no_args_is_help=True,
    help='Manage descriptions of the experiments in a collection.',
    # chain=os.environ.get('_SEML_COMPLETE')
)
app.add_typer(app_description, name='description')


@app.command('detect-duplicates')
@restrict_collection()
def detect_duplicates_command(
    ctx: typer.Context,
    filter_states: FilterStatesAnnotation = [
        *States.STAGED,
        *States.FAILED,
        *States.KILLED,
        *States.INTERRUPTED,
    ],
    filter_dict: FilterDictAnnotation = None,
    batch_id: BatchIdAnnotation = None,
):
    """
    Prints duplicate experiment configurations.
    """
    print_duplicates(
        ctx.obj['collection'],
        filter_states=filter_states,
        filter_dict=filter_dict,
        batch_id=batch_id,
    )


@app_description.command('set')
@restrict_collection()
def description_set_command(
    ctx: typer.Context,
    description: Annotated[
        str,
        typer.Argument(
            help='The description to set.',
        ),
    ],
    sacred_id: SacredIdAnnotation = None,
    filter_states: FilterStatesAnnotation = [],
    filter_dict: FilterDictAnnotation = None,
    batch_id: BatchIdAnnotation = None,
    yes: YesAnnotation = False,
    no_resolve_description: NoResolveDescriptionAnnotation = False,
):
    """
    Sets the description of experiment(s).
    """
    collection_set_description(
        ctx.obj['collection'],
        description,
        sacred_id=sacred_id,
        filter_states=filter_states,
        filter_dict=filter_dict,
        batch_id=batch_id,
        yes=yes,
        resolve=not no_resolve_description,
    )


@app_description.command('delete')
@restrict_collection()
def description_delete_command(
    ctx: typer.Context,
    sacred_id: SacredIdAnnotation = None,
    filter_states: FilterStatesAnnotation = [],
    filter_dict: FilterDictAnnotation = None,
    batch_id: BatchIdAnnotation = None,
    yes: YesAnnotation = False,
):
    """
    Deletes the description of experiment(s).
    """
    collection_delete_description(
        ctx.obj['collection'],
        sacred_id=sacred_id,
        filter_states=filter_states,
        filter_dict=filter_dict,
        batch_id=batch_id,
        yes=yes,
    )


@app_description.command('list')
@restrict_collection()
def description_list_command(
    ctx: typer.Context, update_status: UpdateStatusAnnotation = False
):
    """
    Lists the descriptions of all experiments.
    """
    collection_list_descriptions(ctx.obj['collection'], update_status=update_status)


app_project = typer.Typer(
    no_args_is_help=True,
    help='Setting up new projects.',
)
app.add_typer(app_project, name='project')


@app_project.command('init')
@restrict_collection(False)
def init_project_command(
    ctx: typer.Context,
    directory: Annotated[
        str,
        typer.Argument(
            help='The directory in which to initialize the project.',
            exists=False,
            file_okay=False,
            dir_okay=True,
        ),
    ] = '.',
    template: Annotated[
        str,
        typer.Option(
            '-t',
            '--template',
            help='The template to use for the project. To view available templates use `seml project list-templates`.',
        ),
    ] = 'default',
    project_name: Annotated[
        Optional[str],
        typer.Option(
            '-n',
            '--name',
            help='The name of the project. (By default inferred from the directory name.)',
        ),
    ] = None,
    user_name: Annotated[
        Optional[str],
        typer.Option(
            '-u',
            '--username',
            help='The author name to use for the project. (By default inferred from $USER)',
        ),
    ] = None,
    user_mail: Annotated[
        Optional[str],
        typer.Option(
            '-m',
            '--usermail',
            help='The author email to use for the project. (By default empty.)',
        ),
    ] = None,
    git_remote: Annotated[
        Optional[str],
        typer.Option(
            '-r',
            '--git-remote',
            help='The git remote to use for the project. (By default SETTINGS.TEMPLATE_REMOTE.)',
        ),
    ] = None,
    git_commit: Annotated[
        Optional[str],
        typer.Option(
            '-c',
            '--git-commit',
            help='The exact git commit to use. May also be a tag or branch (By default latest)',
        ),
    ] = None,
    yes: YesAnnotation = False,
):
    """
    Initialize a new project in the given directory.
    """
    init_project(
        directory,
        project_name,
        user_name,
        user_mail,
        template,
        git_remote,
        git_commit,
        yes,
    )


@app_project.command('list-templates')
@restrict_collection(False)
def list_templates_command(
    ctx: typer.Context,
    git_remote: Annotated[
        Optional[str],
        typer.Option(
            '-r',
            '--git-remote',
            help='The git remote to use for the project. (By default SETTINGS.TEMPLATE_REMOTE.)',
        ),
    ] = None,
    git_commit: Annotated[
        Optional[str],
        typer.Option(
            '-c',
            '--git-commit',
            help='The exact git commit to use. May also be a tag or branch (By default latest)',
        ),
    ] = None,
):
    """
    List available project templates.
    """
    print_available_templates(git_remote, git_commit)


@dataclass
class CommandTreeNode:
    """Compact representation of the commands (and subtyper commands) of the app"""

    commands: Set[str]
    groups: Dict[str, 'CommandTreeNode']


@functools.lru_cache()
def command_tree(app: typer.Typer) -> CommandTreeNode:
    return CommandTreeNode(
        commands={
            cmd.name if cmd.name else cmd.callback.__name__
            for cmd in app.registered_commands
        },
        groups={
            (group.name if group.name else group.callback.__name__): command_tree(
                group.typer_instance
            )
            for group in app.registered_groups
        },
    )


def split_args(
    args: List[str], command_tree: CommandTreeNode, combine: bool = True
) -> Tuple[List[List[str]], List[str]]:
    split_cmd_args = [[]]
    cmd_stack = [command_tree]

    # Chaining is only allowed in the first level of the group hierarchy, so we only
    # split into a two level list
    for arg in args:
        if arg in cmd_stack[-1].groups:
            if len(cmd_stack) == 1:  # new subtyper at the top level
                split_cmd_args.append([arg])
                # chaining is allowed: stack[-1] may consume further commands after its child is done consuming
                cmd_stack.append(cmd_stack[-1].groups[arg])
            else:
                split_cmd_args[-1].append(arg)
                # no chaining below the first level: stack[-1] will not consume any more commands
                cmd_stack = cmd_stack[:-1] + [cmd_stack[-1].groups[arg]]
        elif arg in cmd_stack[-1].commands:
            if len(cmd_stack) == 1:  # new command at the top level
                split_cmd_args.append([arg])
            else:
                split_cmd_args[-1].append(arg)
                # no chaining below the first level: stack[-1] will not consume any more commands
                cmd_stack.pop()
        else:
            split_cmd_args[-1].append(arg)

    # Re-distribute shared args to each command in the first level of the hierarchy
    if len(split_cmd_args) == 1:
        return split_cmd_args, cmd_stack
    shared = split_cmd_args[0]
    chained_commands = split_cmd_args[1:]
    # If none of the shared args contains a collection
    # name, we add the default collection name.
    if all(arg.startswith('-') for arg in shared):
        shared.append('')
    # Combine commands with the shared arguments
    result = []
    for split in chained_commands:
        result.append(shared + split)

    return result, cmd_stack


def main():
    # We have to split the arguments manually to get proper chaining.
    # If we were to use typer built-in chaining, lists would end the chain.
    for args in split_args(sys.argv[1:], command_tree(app))[0]:
        # The app will typically exit after running once.
        # We want to run it multiple times, so we catch the SystemExit exception.
        from seml.console import console

        try:
            if len(args) >= 2:
                cmd = args[1]
            else:
                cmd = None
            with console.status(f'Running command: {cmd}'):
                app(args)
        except SystemExit as e:
            if e.code == 0:
                continue
            else:
                raise e


if __name__ == '__main__':
    main()


# If we are in autcompletion we must apply our parameter splitting
# to get correct autocompletion suggestions.
if os.environ.get('_SEML_COMPLETE') and os.environ.get('COMP_WORDS'):
    commands, stack = split_args(
        os.environ['COMP_WORDS'].split('\n'), command_tree(app)
    )
    cword = int(os.environ['COMP_CWORD'])
    if cword > 1:
        # Case where we complete a command
        # To find the right command, we must subtract the length of all previous commands
        # let's subtract -2 everywhere for seml <collection>
        cword -= 2
        for cmd in commands:
            cmd_length = len(cmd) - 2
            # We found our current command
            if cmd_length >= cword:
                break
            cword -= cmd_length
        cword += 2  # add back the -2 we subtracted above
    else:
        # If we complete collection names
        cmd = commands[0]
    os.environ['COMP_WORDS'] = '\n'.join(cmd)
    os.environ['COMP_CWORD'] = str(cword)
    # If we are not at the top level typer, we must not suggest top level commands
    # Note: `seml collection description list <tab><tab>` does not correctly autocomplete
    # as chaining is disabled on the app_description typer. However, if one were to enable
    # that its assumptions about chaining differs from our assumptions about chaining.
    app.info.chain = len(stack) == 1
