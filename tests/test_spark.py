# Env:
#   AWS_ACCESS_KEY_ID
#   AWS_SECRET_ACCESS_KEY
#   COMMONS_DIR
#   S3_BUCKET
#   S3_PREFIX
#   TEST_JAR_PATH // /path/to/mesos-spark-integration-tests.jar
#   SCALA_TEST_JAR_PATH // /path/to/dcos-spark-scala-tests.jar

import logging
import os
import pytest
import json
import shakedown

import sdk_utils

from tests import s3
from tests import utils


LOGGER = logging.getLogger(__name__)
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SPARK_PI_FW_NAME = "Spark Pi"
CNI_TEST_NUM_EXECUTORS = 1
SECRET_NAME = "secret"
SECRET_CONTENTS = "mgummelt"


def setup_module(module):
    utils.require_spark()
    utils.upload_file(os.environ["SCALA_TEST_JAR_PATH"])
    shakedown.run_dcos_command('package install --cli dcos-enterprise-cli --yes')


def teardown_module(module):
    utils.teardown_spark()


@pytest.mark.sanity
def test_jar(app_name="/spark"):
    master_url = ("https" if utils.is_strict() else "http") + "://leader.mesos:5050"
    spark_job_runner_args = '{} dcos \\"*\\" spark:only 2 --auth-token={}'.format(
        master_url,
        shakedown.dcos_acs_token())
    jar_url = utils.upload_file(os.getenv('TEST_JAR_PATH'))
    utils.run_tests(app_url=jar_url,
                    app_args=spark_job_runner_args,
                    expected_output="All tests passed",
                    app_name=app_name,
                    args=["--class", 'com.typesafe.spark.test.mesos.framework.runners.SparkJobRunner'])


@pytest.mark.sanity
def test_sparkPi():
    utils.run_tests(app_url=utils.SPARK_EXAMPLES,
                    app_args="100",
                    expected_output="Pi is roughly 3",
                    app_name="/spark",
                    args=["--class org.apache.spark.examples.SparkPi"])


@pytest.mark.sanity
def test_python():
    python_script_path = os.path.join(THIS_DIR, 'jobs', 'python', 'pi_with_include.py')
    python_script_url = utils.upload_file(python_script_path)
    py_file_path = os.path.join(THIS_DIR, 'jobs', 'python', 'PySparkTestInclude.py')
    py_file_url = utils.upload_file(py_file_path)
    utils.run_tests(app_url=python_script_url,
                    app_args="30",
                    expected_output="Pi is roughly 3",
                    app_name="/spark",
                    args=["--py-files", py_file_url])


@pytest.mark.sanity
def test_r():
    r_script_path = os.path.join(THIS_DIR, 'jobs', 'R', 'dataframe.R')
    r_script_url = utils.upload_file(r_script_path)
    utils.run_tests(app_url=r_script_url,
                    app_args='',
                    expected_output="Justin",
                    app_name="/spark")


@pytest.mark.sanity
def test_cni():
    utils.run_tests(app_url=utils.SPARK_EXAMPLES,
                    app_args="",
                    expected_output="Pi is roughly 3",
                    app_name="/spark",
                    args=["--conf", "spark.mesos.network.name=dcos",
                          "--class", "org.apache.spark.examples.SparkPi"])


#@pytest.mark.skip("Enable when SPARK-21694 is merged and released in DC/OS Spark")
@pytest.mark.sanity
def test_cni_labels():
    driver_task_id = utils.submit_job(app_url=utils.SPARK_EXAMPLES,
                                      app_args="3000",   # Long enough to examine the Driver's & Executor's task infos
                                      app_name="/spark",
                                      args=["--conf", "spark.mesos.network.name=dcos",
                                            "--conf", "spark.mesos.network.labels=key1:val1,key2:val2",
                                            "--conf", "spark.cores.max={}".format(CNI_TEST_NUM_EXECUTORS),
                                            "--class", "org.apache.spark.examples.SparkPi"])

    # Wait until executors are running
    utils.wait_for_executors_running(SPARK_PI_FW_NAME, CNI_TEST_NUM_EXECUTORS)

    # Check for network name / labels in Driver task info
    driver_task = shakedown.get_task(driver_task_id, completed=False)
    _check_task_network_info(driver_task)

    # Check for network name / labels in Executor task info
    executor_task = shakedown.get_service_tasks(SPARK_PI_FW_NAME)[0]
    _check_task_network_info(executor_task)

    # Check job output
    utils.check_job_output(driver_task_id, "Pi is roughly 3")


def _check_task_network_info(task):
    # Expected: "network_infos":[{
    #   "name":"dcos",
    #   "labels":{
    #       "labels":[
    #           {"key":"key1","value":"val1"},
    #           {"key":"key2","value":"val2"}]}}]
    network_info = task['container']['network_infos'][0]
    assert network_info['name'] == "dcos"
    labels = network_info['labels']['labels']
    assert len(labels) == 2
    assert labels[0]['key'] == "key1"
    assert labels[0]['value'] == "val1"
    assert labels[1]['key'] == "key2"
    assert labels[1]['value'] == "val2"


@pytest.mark.sanity
def test_s3():
    linecount_path = os.path.join(THIS_DIR, 'resources', 'linecount.txt')
    s3.upload_file(linecount_path)

    app_args = "--readUrl {} --writeUrl {}".format(
        s3.s3n_url('linecount.txt'),
        s3.s3n_url("linecount-out"))

    args = ["--conf",
            "spark.mesos.driverEnv.AWS_ACCESS_KEY_ID={}".format(
                os.environ["AWS_ACCESS_KEY_ID"]),
            "--conf",
            "spark.mesos.driverEnv.AWS_SECRET_ACCESS_KEY={}".format(
                os.environ["AWS_SECRET_ACCESS_KEY"]),
            "--class", "S3Job"]
    utils.run_tests(app_url=utils._scala_test_jar_url(),
                    app_args=app_args,
                    expected_output="Read 3 lines",
                    app_name="/spark",
                    args=args)

    assert len(list(s3.list("linecount-out"))) > 0

    app_args = "--readUrl {} --countOnly".format(s3.s3n_url('linecount.txt'))

    args = ["--conf",
            "spark.mesos.driverEnv.AWS_ACCESS_KEY_ID={}".format(
                os.environ["AWS_ACCESS_KEY_ID"]),
            "--conf",
            "spark.mesos.driverEnv.AWS_SECRET_ACCESS_KEY={}".format(
                os.environ["AWS_SECRET_ACCESS_KEY"]),
            "--class", "S3Job"]
    utils.run_tests(app_url=utils._scala_test_jar_url(),
                    app_args=app_args,
                    expected_output="Read 3 lines",
                    app_name="/spark",
                    args=args)

    app_args = "--countOnly --readUrl {}".format(s3.s3n_url('linecount.txt'))

    args = ["--conf",
            "spark.mesos.driverEnv.AWS_ACCESS_KEY_ID={}".format(
                os.environ["AWS_ACCESS_KEY_ID"]),
            "--conf",
            "spark.mesos.driverEnv.AWS_SECRET_ACCESS_KEY={}".format(
                os.environ["AWS_SECRET_ACCESS_KEY"]),
            "--class", "S3Job"]
    utils.run_tests(app_url=utils._scala_test_jar_url(),
                    app_args=app_args,
                    expected_output="Read 3 lines",
                    app_name="/spark",
                    args=args)


# Skip DC/OS < 1.10, because it doesn't have adminrouter support for service groups.
@pytest.mark.skipif('shakedown.dcos_version_less_than("1.10")')
@pytest.mark.sanity
def test_marathon_group():
    app_id = "/path/to/spark"
    options = {"service": {"name": app_id}}
    utils.require_spark(options=options, service_name=app_id)
    test_jar(app_name=app_id)
    LOGGER.info("Uninstalling app_id={}".format(app_id))
    #shakedown.uninstall_package_and_wait(SPARK_PACKAGE_NAME, app_id)


@pytest.mark.sanity
@pytest.mark.secrets
def test_secrets():
    properties_file_path = os.path.join(THIS_DIR, "resources", "secrets-opts.txt")
    # Create secret
    shakedown.run_dcos_command('security secrets create /{} --value {}'.format(SECRET_NAME, SECRET_CONTENTS))

    secret_file_name = "secret_file"
    output = "Contents of file {}: {}".format(secret_file_name, SECRET_CONTENTS)
    args = ["--properties-file", properties_file_path,
            "--class", "SecretsJob"]
    try:
        utils.run_tests(app_url=utils._scala_test_jar_url(),
                        app_args=secret_file_name,
                        expected_output=output,
                        app_name="/spark",
                        args=args)

    finally:
        # Delete secret
        shakedown.run_dcos_command('security secrets delete /{}'.format(SECRET_NAME))


@pytest.mark.sanity
def test_cli_multiple_spaces():
    utils.run_tests(app_url=utils.SPARK_EXAMPLES,
                    app_args="30",
                    expected_output="Pi is roughly 3",
                    app_name="/spark",
                    args=["--conf ", "spark.cores.max=2",
                          " --class  ", "org.apache.spark.examples.SparkPi"])


# Skip DC/OS < 1.10, because it doesn't have support for file-based secrets.
@pytest.mark.skipif('shakedown.dcos_version_less_than("1.10")')
@sdk_utils.dcos_ee_only
@pytest.mark.sanity
def test_driver_executor_tls():
    '''
    Put keystore and truststore as secrets in DC/OS secret store.
    Run SparkPi job with TLS enabled, referencing those secrets.
    Make sure other secrets still show up.
    '''
    python_script_path = os.path.join(THIS_DIR, 'jobs', 'python', 'pi_with_secret.py')
    python_script_url = utils.upload_file(python_script_path)
    resources_folder = os.path.join(
        os.path.dirname(os.path.realpath(__file__)), 'resources'
    )
    keystore_file = 'server.jks'
    truststore_file = 'trust.jks'
    keystore_path = os.path.join(resources_folder, '{}.base64'.format(keystore_file))
    truststore_path = os.path.join(resources_folder, '{}.base64'.format(truststore_file))
    keystore_secret = '__dcos_base64__keystore'
    truststore_secret = '__dcos_base64__truststore'
    my_secret = 'mysecret'
    my_secret_content = 'secretcontent'
    shakedown.run_dcos_command('security secrets create /{} --value-file {}'.format(keystore_secret, keystore_path))
    shakedown.run_dcos_command('security secrets create /{} --value-file {}'.format(truststore_secret, truststore_path))
    shakedown.run_dcos_command('security secrets create /{} --value {}'.format(my_secret, my_secret_content))
    password = 'changeit'
    try:
        utils.run_tests(app_url=python_script_url,
                        app_args="30 {} {}".format(my_secret, my_secret_content),
                        expected_output="Pi is roughly 3",
                        app_name="/spark",
                        args=["--keystore-secret-path", keystore_secret,
                              "--truststore-secret-path", truststore_secret,
                              "--private-key-password", format(password),
                              "--keystore-password", format(password),
                              "--truststore-password", format(password),
                              "--conf", "spark.mesos.driver.secret.names={}".format(my_secret),
                              "--conf", "spark.mesos.driver.secret.filenames={}".format(my_secret),
                              "--conf", "spark.mesos.driver.secret.envkeys={}".format(my_secret),
                              ])
    finally:
        shakedown.run_dcos_command('security secrets delete /{}'.format(keystore_secret))
        shakedown.run_dcos_command('security secrets delete /{}'.format(truststore_secret))
        shakedown.run_dcos_command('security secrets delete /{}'.format(my_secret))


def _scala_test_jar_url():
    return s3.http_url(os.path.basename(os.environ["SCALA_TEST_JAR_PATH"]))