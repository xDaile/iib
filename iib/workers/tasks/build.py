# SPDX-License-Identifier: GPL-3.0-or-later
import logging
import os
import tempfile
import textwrap

from iib.exceptions import IIBError
from iib.workers.api_utils import set_request_state, update_request
from iib.workers.config import get_worker_config
from iib.workers.tasks.celery import app
from iib.workers.tasks.legacy import (
    get_legacy_support_packages,
    opm_index_export,
    validate_legacy_params_and_config,
)
from iib.workers.tasks.utils import get_image_labels, run_cmd, skopeo_inspect


__all__ = ['handle_add_request', 'handle_rm_request']

log = logging.getLogger(__name__)


def _build_image(dockerfile_dir, request_id, arch):
    """
    Build the index image for the specified architecture.

    :param str dockerfile_dir: the path to the directory containing the data generated by the
        opm command
    :param int request_id: the ID of the IIB build request
    :param str arch: the architecture to build this image for
    :raises iib.exceptions.IIBError: if the build fails
    """
    destination = _get_local_pull_spec(request_id, arch)
    log.info('Building the index image for arch %s and tagging it as %s', arch, destination)
    dockerfile_path = os.path.join(dockerfile_dir, 'index.Dockerfile')
    run_cmd(
        [
            'buildah',
            'bud',
            '--no-cache',
            '--override-arch',
            arch,
            '-t',
            destination,
            '-f',
            dockerfile_path,
        ],
        {'cwd': dockerfile_dir},
        exc_msg=f'Failed to build the index image on the arch {arch}',
    )


def _cleanup():
    """
    Remove all existing container images on the host.

    This will ensure that the host will not run out of disk space due to stale data, and that
    all images referenced using floating tags will be up to date on the host.

    :raises iib.exceptions.IIBError: if the command to remove the images fails
    """
    log.info('Removing all existing container images')
    run_cmd(
        ['podman', 'rmi', '--all', '--force'],
        exc_msg='Failed to remove the existing container images',
    )


def _create_and_push_manifest_list(request_id, arches):
    """
    Create and push the manifest list to the configured registry.

    :param int request_id: the ID of the IIB build request
    :param iter arches: an iterable of arches to create the manifest list for
    :return: the pull specification of the manifest list
    :rtype: str
    :raises iib.exceptions.IIBError: if creating or pushing the manifest list fails
    """
    output_pull_spec = get_rebuilt_index_image(request_id)
    log.info('Creating the manifest list %s', output_pull_spec)
    with tempfile.TemporaryDirectory(prefix='iib-') as temp_dir:
        manifest_yaml = os.path.abspath(os.path.join(temp_dir, 'manifest.yaml'))
        with open(manifest_yaml, 'w+') as manifest_yaml_f:
            manifest_yaml_f.write(
                textwrap.dedent(
                    f'''\
                    image: {output_pull_spec}
                    manifests:
                    '''
                )
            )
            for arch in sorted(arches):
                arch_pull_spec = _get_external_arch_pull_spec(request_id, arch)
                log.debug(
                    'Adding the manifest %s to the manifest list %s',
                    arch_pull_spec,
                    output_pull_spec,
                )
                manifest_yaml_f.write(
                    textwrap.dedent(
                        f'''\
                        - image: {arch_pull_spec}
                          platform:
                            architecture: {arch}
                            os: linux
                        '''
                    )
                )
            # Return back to the beginning of the file to output it to the logs
            manifest_yaml_f.seek(0)
            log.debug(
                'Created the manifest configuration with the following content:\n%s',
                manifest_yaml_f.read(),
            )

        run_cmd(
            ['manifest-tool', 'push', 'from-spec', manifest_yaml],
            exc_msg=f'Failed to push the manifest list to {output_pull_spec}',
        )

    return output_pull_spec


def _finish_request_post_build(output_pull_spec, request_id, arches):
    """
    Finish the request after the manifest list has been pushed.

    This function was created so that code didn't need to be duplicated for the ``add`` and ``rm``
    request types.

    :param str output_pull_spec: pull spec of the index image generated by IIB
    :param int request_id: the ID of the IIB build request
    :param set arches: the set of arches that were built as part of this request
    :raises iib.exceptions.IIBError: if the manifest list couldn't be created and pushed
    """
    payload = {
        'arches': list(arches),
        'index_image': output_pull_spec,
        'state': 'complete',
        'state_reason': 'The request completed successfully',
    }
    update_request(request_id, payload, exc_msg='Failed setting the index image on the request')


def _get_external_arch_pull_spec(request_id, arch, include_transport=False):
    """
    Get the pull specification of the single arch image in the external registry.

    :param int request_id: the ID of the IIB build request
    :param str arch: the specific architecture of the image
    :param bool include_transport: if true, `docker://` will be prefixed in the returned pull
        specification
    :return: the pull specification of the single arch image in the external registry
    :rtype: str
    """
    pull_spec = get_rebuilt_index_image(request_id) + f'-{arch}'
    if include_transport:
        return f'docker://{pull_spec}'
    return pull_spec


def _get_local_pull_spec(request_id, arch):
    """
    Get the local pull specification of the architecture specfic index image for this request.

    :return: the pull specification of the index image for this request.
    :param str arch: the specific architecture of the image.
    :rtype: str
    """
    return f'iib-build:{request_id}-{arch}'


def _get_image_arches(pull_spec):
    """
    Get the architectures this image was built for.

    :param str pull_spec: the pull specification to a v2 manifest list
    :return: a set of architectures of the images contained in the manifest list
    :rtype: set
    :raises iib.exceptions.IIBError: if the pull specification is not a v2 manifest list
    """
    log.debug('Get the available arches for %s', pull_spec)
    skopeo_raw = skopeo_inspect(f'docker://{pull_spec}', '--raw')
    arches = set()
    if skopeo_raw['mediaType'] == 'application/vnd.docker.distribution.manifest.list.v2+json':
        for manifest in skopeo_raw['manifests']:
            arches.add(manifest['platform']['architecture'])
    elif skopeo_raw['mediaType'] == 'application/vnd.docker.distribution.manifest.v2+json':
        skopeo_out = skopeo_inspect(f'docker://{pull_spec}')
        arches.add(skopeo_out['Architecture'])
    else:
        raise IIBError(
            f'The pull specification of {pull_spec} is neither a v2 manifest list nor a v2 manifest'
        )

    return arches


def get_rebuilt_index_image(request_id):
    """
    Generate the pull specification of the index image rebuilt by IIB.

    :param int request_id: the ID of the IIB build request
    :return: pull specification of the rebuilt index image
    :rtype: str
    """
    conf = get_worker_config()
    return conf['iib_image_push_template'].format(
        registry=conf['iib_registry'], request_id=request_id
    )


def _get_resolved_image(pull_spec):
    """
    Get the pull specification of the image using its digest.

    :param str pull_spec: the pull specification of the image to resolve
    :return: the resolved pull specification
    :rtype: str
    """
    log.debug('Resolving %s', pull_spec)
    skopeo_output = skopeo_inspect(f'docker://{pull_spec}')
    pull_spec_resolved = f'{skopeo_output["Name"]}@{skopeo_output["Digest"]}'
    log.debug('%s resolved to %s', pull_spec, pull_spec_resolved)
    return pull_spec_resolved


def _opm_index_add(base_dir, bundles, binary_image, from_index=None):
    """
    Add the input bundles to an operator index.

    This only produces the index.Dockerfile file and does not build the image.

    :param str base_dir: the base directory to generate the database and index.Dockerfile in.
    :param list bundles: a list of strings representing the pull specifications of the bundles to
        add to the index image being built.
    :param str binary_image: the pull specification of the image where the opm binary gets copied
        from. This should point to a digest or stable tag.
    :param str from_index: the pull specification of the image containing the index that the index
        image build will be based from.
    :raises iib.exceptions.IIBError: if the ``opm index add`` command fails.
    """
    # The bundles are not resolved since these are stable tags, and references
    # to a bundle image using a digest fails when using the opm command.
    cmd = [
        'opm',
        'index',
        'add',
        '--generate',
        '--bundles',
        ','.join(bundles),
        '--binary-image',
        binary_image,
    ]

    log.info('Generating the database file with the following bundle(s): %s', ', '.join(bundles))
    if from_index:
        log.info('Using the existing database from %s', from_index)
        # from_index is not resolved because podman does not support digest references
        # https://github.com/containers/libpod/issues/5234 is filed for it
        cmd.extend(['--from-index', from_index])

    run_cmd(
        cmd, {'cwd': base_dir}, exc_msg='Failed to add the bundles to the index image',
    )


def _opm_index_rm(base_dir, operators, binary_image, from_index):
    """
    Remove the input operators from the operator index.

    This only produces the index.Dockerfile file and does not build the image.

    :param str base_dir: the base directory to generate the database and index.Dockerfile in.
    :param list operators: a list of strings representing the names of the operators to
        remove from the index image.
    :param str binary_image: the pull specification of the image where the opm binary gets copied
        from.
    :param str from_index: the pull specification of the image containing the index that the index
        image build will be based from.
    :raises iib.exceptions.IIBError: if the ``opm index rm`` command fails.
    """
    cmd = [
        'opm',
        'index',
        'rm',
        '--generate',
        '--binary-image',
        binary_image,
        '--from-index',
        from_index,
        '--operators',
        ','.join(operators),
    ]

    log.info(
        'Generating the database file from an existing database %s and excluding'
        ' the following operator(s): %s',
        from_index,
        ', '.join(operators),
    )

    run_cmd(
        cmd, {'cwd': base_dir}, exc_msg='Failed to remove operators from the index image',
    )


def _prepare_request_for_build(
    binary_image, request_id, from_index=None, add_arches=None, bundles=None
):
    """
    Prepare the request for the index image build.

    All information that was retrieved and/or calculated for the next steps in the build are
    returned as a dictionary.

    This function was created so that code didn't need to be duplicated for the ``add`` and ``rm``
    request types.

    :param str binary_image: the pull specification of the image where the opm binary gets copied
        from.
    :param int request_id: the ID of the IIB build request
    :param str from_index: the pull specification of the image containing the index that the index
        image build will be based from.
    :param list add_arches: the list of arches to build in addition to the arches ``from_index`` is
        currently built for; if ``from_index`` is ``None``, then this is used as the list of arches
        to build the index image for
    :param list bundles: the list of bundles to create the bundle mapping on the request
    :return: a dictionary with the keys: arches, binary_image_resolved, and fom_index_resolved.
    :raises iib.exceptions.IIBError: if the image resolution fails or the architectures couldn't
        be detected.
    """
    if bundles is None:
        bundles = []

    set_request_state(request_id, 'in_progress', 'Resolving the images')

    if add_arches:
        arches = set(add_arches)
    else:
        arches = set()

    binary_image_resolved = _get_resolved_image(binary_image)
    binary_image_arches = _get_image_arches(binary_image_resolved)

    if from_index:
        from_index_resolved = _get_resolved_image(from_index)
        from_index_arches = _get_image_arches(from_index_resolved)
        arches = arches | from_index_arches
    else:
        from_index_resolved = None

    if not arches:
        raise IIBError('No arches were provided to build the index image')

    arches_str = ', '.join(sorted(arches))
    log.debug('Set to build the index image for the following arches: %s', arches_str)

    if not arches.issubset(binary_image_arches):
        raise IIBError(
            'The binary image is not available for the following arches: {}'.format(
                ', '.join(sorted(arches - binary_image_arches))
            )
        )

    bundle_mapping = {}
    for bundle in bundles:
        operator = get_image_label(bundle, 'operators.operatorframework.io.bundle.package.v1')
        if operator:
            bundle_mapping.setdefault(operator, []).append(bundle)

    payload = {
        'binary_image_resolved': binary_image_resolved,
        'bundle_mapping': bundle_mapping,
        'state': 'in_progress',
        'state_reason': f'Building the index image for the following arches: {arches_str}',
    }
    if from_index_resolved:
        payload['from_index_resolved'] = from_index_resolved
    exc_msg = 'Failed setting the resolved images on the request'
    update_request(request_id, payload, exc_msg)

    return {
        'arches': arches,
        'binary_image_resolved': binary_image_resolved,
        'from_index_resolved': from_index_resolved,
    }


def _push_image(request_id, arch):
    """
    Push the single arch index image to the configured registry.

    :param int request_id: the ID of the IIB build request
    :param str arch: the architecture of the image to push
    :raises iib.exceptions.IIBError: if the push fails
    """
    source = _get_local_pull_spec(request_id, arch)
    destination = _get_external_arch_pull_spec(request_id, arch, include_transport=True)
    log.info('Pushing the index image %s to %s', source, destination)
    run_cmd(
        ['podman', 'push', '-q', source, destination],
        exc_msg=f'Failed to push the index image to {destination} for the arch {arch}',
    )


def _verify_index_image(resolved_prebuild_from_index, unresolved_from_index):
    """
    Verify if the index image has changed since the IIB build request started

    :param str resolved_prebuild_from_index: resolved index image before starting the build
    :param str unresolved_from_index: unresolved index image provided as API input
    :raises iib.exceptions.IIBError: if the index image has changed since IIB build started.
    """
    resolved_post_build_from_index = _get_resolved_image(unresolved_from_index)
    if resolved_post_build_from_index != resolved_prebuild_from_index:
        raise IIBError(
            'The supplied from_index image changed during the IIB request.'
            ' Please resubmit the request.'
        )


def _verify_labels(bundles):
    """
    Verify that the required labels are set on the input bundles.

    :param list bundles: a list of strings representing the pull specifications of the bundles to
        add to the index image being built.
    :raises iib.exceptions.IIBError: if one of the bundles does not have the correct label value.
    """
    conf = get_worker_config()
    if not conf['iib_required_labels']:
        return

    for bundle in bundles:
        labels = get_image_labels(bundle)
        for label, value in conf['iib_required_labels'].items():
            if labels.get(label) != value:
                raise IIBError(f'The bundle {bundle} does not have the label {label}={value}')


def get_image_label(pull_spec, label):
    """
    Get a specific label from the image.

    :param str label: the label to get
    :return: the label on the image or None
    :rtype: str
    """
    log.debug('Getting the label of %s from %s', label, pull_spec)
    return get_image_labels(pull_spec).get(label)


@app.task
def handle_add_request(
    bundles,
    binary_image,
    request_id,
    from_index=None,
    add_arches=None,
    cnr_token=None,
    organization=None,
):
    """
    Coordinate the the work needed to build the index image with the input bundles.

    :param list bundles: a list of strings representing the pull specifications of the bundles to
        add to the index image being built.
    :param str binary_image: the pull specification of the image where the opm binary gets copied
        from.
    :param int request_id: the ID of the IIB build request
    :param str from_index: the pull specification of the image containing the index that the index
        image build will be based from.
    :param list add_arches: the list of arches to build in addition to the arches ``from_index`` is
        currently built for; if ``from_index`` is ``None``, then this is used as the list of arches
        to build the index image for
    :param str cnr_token: the token required to push backported packages to the legacy
        app registry via OMPS.
    :param str organization: organization name in the legacy app registry to which the backported
        packages should be pushed to.
    :raises iib.exceptions.IIBError: if the index image build fails or legacy support is required
        and one of ``cnr_token`` or ``organization`` is not specified.
    """
    _verify_labels(bundles)

    legacy_support_packages = get_legacy_support_packages(bundles)
    if legacy_support_packages:
        validate_legacy_params_and_config(legacy_support_packages, bundles, cnr_token, organization)

    _cleanup()
    prebuild_info = _prepare_request_for_build(
        binary_image, request_id, from_index, add_arches, bundles
    )

    with tempfile.TemporaryDirectory(prefix='iib-') as temp_dir:
        _opm_index_add(temp_dir, bundles, prebuild_info['binary_image_resolved'], from_index)

        arches = prebuild_info['arches']
        for arch in sorted(arches):
            _build_image(temp_dir, request_id, arch)
            _push_image(request_id, arch)

    if from_index:
        _verify_index_image(prebuild_info['from_index_resolved'], from_index)

    set_request_state(request_id, 'in_progress', 'Creating the manifest list')
    output_pull_spec = _create_and_push_manifest_list(request_id, arches)

    if legacy_support_packages:
        opm_index_export(
            legacy_support_packages, request_id, output_pull_spec, cnr_token, organization
        )

    _finish_request_post_build(output_pull_spec, request_id, arches)


@app.task
def handle_rm_request(operators, binary_image, request_id, from_index, add_arches=None):
    """
    Coordinate the work needed to remove the input operators and rebuild the index image.

    :param list operators: a list of strings representing the name of the operators to
        remove from the index image.
    :param str binary_image: the pull specification of the image where the opm binary gets copied
        from.
    :param int request_id: the ID of the IIB build request
    :param str from_index: the pull specification of the image containing the index that the index
        image build will be based from.
    :param list add_arches: the list of arches to build in addition to the arches ``from_index`` is
        currently built for.
    :raises iib.exceptions.IIBError: if the index image build fails.
    """
    _cleanup()
    prebuild_info = _prepare_request_for_build(binary_image, request_id, from_index, add_arches)

    with tempfile.TemporaryDirectory(prefix='iib-') as temp_dir:
        _opm_index_rm(temp_dir, operators, binary_image, from_index)

        arches = prebuild_info['arches']
        for arch in sorted(arches):
            _build_image(temp_dir, request_id, arch)
            _push_image(request_id, arch)

    _verify_index_image(prebuild_info['from_index_resolved'], from_index)

    set_request_state(request_id, 'in_progress', 'Creating the manifest list')
    output_pull_spec = _create_and_push_manifest_list(request_id, arches)

    _finish_request_post_build(output_pull_spec, request_id, arches)
